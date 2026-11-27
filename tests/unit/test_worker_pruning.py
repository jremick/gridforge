"""Unit tests for bounded LRU cache pruning and role-aware worker TTL cleanup."""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gridforge.config import ColorMode, Orientation, PaperSize
from gridforge.web import cache_prune, db, projects as projects_store, worker
from gridforge.web.projects import ProjectCreateRequest


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_job(
    db_path: Path,
    *,
    job_id: str,
    owner_id: str,
    owner_role: str,
    status: str,
    age_hours: float,
    data_dir: Path | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    ts = _iso(now - timedelta(hours=age_hours))
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
        finished = ts if status in {"succeeded", "failed", "cancelled"} else None
        conn.execute(
            "UPDATE jobs SET status = ?, created_at = ?, finished_at = ? WHERE job_id = ?",
            (status, ts, finished, job_id),
        )
        conn.commit()
    finally:
        conn.close()

    if data_dir is not None:
        jdir = data_dir / job_id / "artifacts"
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / f"{job_id}.pdf").write_bytes(b"%PDF-1.4\n")


def _seed_project(
    db_path: Path,
    *,
    name: str,
    owner_id: str,
    owner_role: str,
    age_hours: float,
) -> str:
    rec = projects_store.create_project(
        db_path,
        ProjectCreateRequest(name=name, state={"scale": 25000}),
        owner_id=owner_id,
        owner_role=owner_role,
        guest_ttl_hours=0.0,
    )
    ts = _iso(datetime.now(timezone.utc) - timedelta(hours=age_hours))
    conn = db._connect(db_path)
    try:
        conn.execute(
            "UPDATE projects SET created_at = ?, updated_at = ? WHERE project_id = ?",
            (ts, ts, rec.project_id),
        )
        conn.commit()
    finally:
        conn.close()
    return rec.project_id


class TestCachePrune:
    def test_zero_qgis_or_pyqt5_imports(self) -> None:
        assert not any(
            mod.startswith(("qgis", "PyQt5")) for mod in sys.modules
        ), "web worker and cache_prune must not import qgis or PyQt5"

    def test_missing_or_non_directory_or_symlink_leaf_returns_zeroed(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        res_missing = cache_prune.trim_cache_dir(missing, 1024)
        assert res_missing == cache_prune.CachePruneResult(0, 0, 0, 0, 0, 0)

        regular_file = tmp_path / "not_a_dir.txt"
        regular_file.write_bytes(b"abc")
        res_file = cache_prune.trim_cache_dir(regular_file, 1024)
        assert res_file == cache_prune.CachePruneResult(0, 0, 0, 0, 0, 0)

        target_dir = tmp_path / "real_target"
        target_dir.mkdir()
        (target_dir / "keep.tif").write_bytes(b"12345")
        link_dir = tmp_path / "symlink_cache"
        link_dir.symlink_to(target_dir, target_is_directory=True)
        res_link = cache_prune.trim_cache_dir(link_dir, 0)
        assert res_link == cache_prune.CachePruneResult(0, 0, 0, 0, 0, 0)
        assert (target_dir / "keep.tif").is_file()

    def test_under_budget_performs_no_deletions(self, tmp_path: Path) -> None:
        cdir = tmp_path / "cache"
        (cdir / "copernicus" / "clips").mkdir(parents=True)
        f1 = cdir / "copernicus" / "clips" / "tile_a.tif"
        f2 = cdir / "copernicus" / "clips" / "tile_b.tif"
        f1.write_bytes(b"a" * 40)
        f2.write_bytes(b"b" * 60)

        res = cache_prune.trim_cache_dir(cdir, 100)
        assert res.total_bytes_before == 100
        assert res.total_bytes_after == 100
        assert res.deleted_files == 0
        assert res.deleted_bytes == 0
        assert res.removed_dirs == 0
        assert f1.is_file() and f2.is_file()

    def test_lru_eviction_by_atime_and_empty_dir_cleanup(self, tmp_path: Path) -> None:
        cdir = tmp_path / "cache"
        old_dir = cdir / "copernicus" / "clips"
        mid_dir = cdir / "overpass"
        new_dir = cdir / "au_overlays"
        old_dir.mkdir(parents=True)
        mid_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)

        f_oldest = old_dir / "oldest.tif"
        f_mid = mid_dir / "mid.osm"
        f_newest = new_dir / "newest.json"
        f_oldest.write_bytes(b"x" * 50)
        f_mid.write_bytes(b"y" * 50)
        f_newest.write_bytes(b"z" * 50)

        # Even if oldest.tif has a newer mtime, its older atime makes it first to evict.
        os.utime(f_oldest, ns=(1_000_000_000, 9_000_000_000))
        os.utime(f_mid, ns=(2_000_000_000, 2_000_000_000))
        os.utime(f_newest, ns=(3_000_000_000, 1_000_000_000))

        res = cache_prune.trim_cache_dir(cdir, 60)
        assert res.total_bytes_before == 150
        assert res.total_bytes_after == 50
        assert res.deleted_files == 2
        assert res.deleted_bytes == 100
        assert res.removed_dirs == 3  # copernicus/clips, copernicus, overpass
        assert not f_oldest.exists()
        assert not f_mid.exists()
        assert f_newest.is_file()
        assert cdir.is_dir()

    def test_mtime_fallback_when_atime_zero_and_tie_break(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cdir = tmp_path / "cache"
        cdir.mkdir()
        f1 = cdir / "older_mtime.tif"
        f2 = cdir / "newer_mtime.tif"
        f1.write_bytes(b"a" * 30)
        f2.write_bytes(b"b" * 30)

        real_stat = os.stat

        def _fake_stat(path: Path | str, *, follow_symlinks: bool = True) -> os.stat_result | SimpleNamespace:
            st = real_stat(path, follow_symlinks=follow_symlinks)
            name = Path(path).name
            if name == "older_mtime.tif":
                return SimpleNamespace(
                    st_mode=st.st_mode,
                    st_size=st.st_size,
                    st_atime_ns=0,
                    st_mtime_ns=100,
                )
            if name == "newer_mtime.tif":
                return SimpleNamespace(
                    st_mode=st.st_mode,
                    st_size=st.st_size,
                    st_atime_ns=0,
                    st_mtime_ns=200,
                )
            return st

        monkeypatch.setattr(cache_prune.os, "stat", _fake_stat)
        res = cache_prune.trim_cache_dir(cdir, 30)
        assert res.deleted_files == 1
        assert not f1.exists()
        assert f2.is_file()

    def test_skips_protected_temp_and_lock_files(self, tmp_path: Path) -> None:
        cdir = tmp_path / "cache"
        sub = cdir / "geofabrik"
        sub.mkdir(parents=True)

        regular = sub / "region.osm.pbf"
        tmp_f = sub / "tile.tif.tmp"
        part_f = sub / "region.osm.pbf.part"
        lock_f = sub / "cache.lock"
        mid_tmp = sub / "data.tmp.123"
        regular.write_bytes(b"r" * 40)
        tmp_f.write_bytes(b"t" * 100)
        part_f.write_bytes(b"p" * 100)
        lock_f.write_bytes(b"l" * 10)
        mid_tmp.write_bytes(b"m" * 20)

        res = cache_prune.trim_cache_dir(cdir, 0)
        assert res.total_bytes_before == 40
        assert res.total_bytes_after == 0
        assert res.deleted_files == 1
        assert res.skipped_files == 4
        assert res.removed_dirs == 0
        assert not regular.exists()
        assert tmp_f.is_file()
        assert part_f.is_file()
        assert lock_f.is_file()
        assert mid_tmp.is_file()
        assert sub.is_dir()

    def test_symlink_safety_never_touches_external_targets(self, tmp_path: Path) -> None:
        cdir = tmp_path / "cache"
        cdir.mkdir()
        ext_dir = tmp_path / "outside_dir"
        ext_dir.mkdir()
        ext_file = tmp_path / "outside.tif"
        ext_sub_file = ext_dir / "nested.tif"
        ext_file.write_bytes(b"e" * 80)
        ext_sub_file.write_bytes(b"s" * 80)

        (cdir / "link_file.tif").symlink_to(ext_file)
        (cdir / "link_dir").symlink_to(ext_dir, target_is_directory=True)
        local = cdir / "local.tif"
        local.write_bytes(b"l" * 20)

        res = cache_prune.trim_cache_dir(cdir, 0)
        assert res.deleted_files == 1
        assert res.skipped_files == 1
        assert not local.exists()
        assert ext_file.is_file()
        assert ext_sub_file.is_file()

    def test_tolerates_unlink_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cdir = tmp_path / "cache"
        cdir.mkdir()
        f1 = cdir / "a_gone.tif"
        f2 = cdir / "b_locked.tif"
        f3 = cdir / "c_ok.tif"
        f1.write_bytes(b"a" * 30)
        f2.write_bytes(b"b" * 30)
        f3.write_bytes(b"c" * 30)
        os.utime(f1, (1000, 1000))
        os.utime(f2, (2000, 2000))
        os.utime(f3, (3000, 3000))

        orig_unlink = Path.unlink

        def _flaky_unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name == "a_gone.tif":
                raise FileNotFoundError("vanished")
            if self.name == "b_locked.tif":
                raise PermissionError("busy")
            orig_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _flaky_unlink)
        res = cache_prune.trim_cache_dir(cdir, 30)
        assert res.deleted_files == 1
        assert res.deleted_bytes == 30
        assert f2.is_file()
        assert not f3.exists()


class TestWorkerPruning:
    def test_run_pruning_pass_hosted_role_aware_and_cache_trim(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "jobs"
        cache_dir = tmp_path / "cache"
        data_dir.mkdir()
        cache_dir.mkdir()
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)

        # Jobs across roles & statuses
        _seed_job(db_path, job_id="guest_old", owner_id="guest:1", owner_role="guest", status="succeeded", age_hours=30.0, data_dir=data_dir)
        _seed_job(db_path, job_id="guest_fresh", owner_id="guest:2", owner_role="guest", status="succeeded", age_hours=2.0, data_dir=data_dir)
        _seed_job(db_path, job_id="guest_running", owner_id="guest:3", owner_role="guest", status="running", age_hours=50.0, data_dir=data_dir)
        _seed_job(db_path, job_id="friend_old", owner_id="usr_f1", owner_role="friend", status="failed", age_hours=350.0, data_dir=data_dir)
        _seed_job(db_path, job_id="friend_fresh", owner_id="usr_f2", owner_role="friend", status="succeeded", age_hours=48.0, data_dir=data_dir)
        _seed_job(db_path, job_id="admin_old", owner_id="usr_admin", owner_role="admin", status="succeeded", age_hours=2000.0, data_dir=data_dir)
        _seed_job(db_path, job_id="admin_fresh", owner_id="usr_admin", owner_role="admin", status="succeeded", age_hours=200.0, data_dir=data_dir)
        _seed_job(db_path, job_id="none_old", owner_id="local", owner_role="none", status="succeeded", age_hours=2000.0, data_dir=data_dir)

        # Projects
        p_guest_old = _seed_project(db_path, name="g_old", owner_id="guest:1", owner_role="guest", age_hours=30.0)
        p_guest_fresh = _seed_project(db_path, name="g_fresh", owner_id="guest:2", owner_role="guest", age_hours=2.0)
        p_friend_old = _seed_project(db_path, name="f_old", owner_id="usr_f1", owner_role="friend", age_hours=350.0)
        p_friend_fresh = _seed_project(db_path, name="f_fresh", owner_id="usr_f2", owner_role="friend", age_hours=48.0)
        p_admin_old = _seed_project(db_path, name="a_old", owner_id="usr_admin", owner_role="admin", age_hours=2000.0)

        # Sessions & invites (expired vs active)
        now = datetime.now(timezone.utc)
        conn = db._connect(db_path)
        try:
            conn.execute(
                "INSERT INTO users (user_id, email, password_hash, display_name, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("usr_f1", "friend@example.com", "hash", "Friend", "friend", _iso(now), _iso(now)),
            )
            conn.execute(
                "INSERT INTO sessions (session_id, token_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("sess_exp", "h1", "usr_f1", _iso(now - timedelta(days=5)), _iso(now - timedelta(hours=1)), _iso(now - timedelta(days=5))),
            )
            conn.execute(
                "INSERT INTO sessions (session_id, token_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("sess_ok", "h2", "usr_f1", _iso(now), _iso(now + timedelta(hours=10)), _iso(now)),
            )
            conn.execute(
                "INSERT INTO invites (invite_id, token_hash, email, role, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("inv_exp", "ih1", None, "friend", _iso(now - timedelta(days=5)), _iso(now - timedelta(hours=1))),
            )
            conn.execute(
                "INSERT INTO invites (invite_id, token_hash, email, role, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("inv_ok", "ih2", None, "friend", _iso(now), _iso(now + timedelta(hours=10))),
            )
            conn.commit()
        finally:
            conn.close()

        # Cache files over 100 bytes
        old_cache = cache_dir / "old.tif"
        new_cache = cache_dir / "new.tif"
        old_cache.write_bytes(b"1" * 80)
        new_cache.write_bytes(b"2" * 80)
        os.utime(old_cache, (1000, 1000))
        os.utime(new_cache, (2000, 2000))

        summary = worker.run_pruning_pass(
            db_path,
            data_dir=data_dir,
            cache_dir=cache_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
            admin_ttl_hours=720.0,
            cache_max_bytes=100,
            auth_mode="hosted",
        )
        assert summary["jobs"] == 3
        assert summary["projects"] == 2
        assert summary["sessions"] == 1
        assert summary["invites"] == 1
        assert summary["cache_deleted_files"] == 1
        assert summary["cache_bytes_after"] == 80

        assert db.get_job(db_path, "guest_old") is None
        assert not (data_dir / "guest_old").exists()
        assert db.get_job(db_path, "friend_old") is None
        assert not (data_dir / "friend_old").exists()
        assert db.get_job(db_path, "admin_old") is None
        assert not (data_dir / "admin_old").exists()
        for kept in ("guest_fresh", "guest_running", "friend_fresh", "admin_fresh", "none_old"):
            assert db.get_job(db_path, kept) is not None

        # Setting admin_ttl_hours=0 preserves even ancient admin jobs
        _seed_job(db_path, job_id="admin_keep", owner_id="usr_admin", owner_role="admin", status="succeeded", age_hours=5000.0, data_dir=data_dir)
        summary_keep = worker.run_pruning_pass(
            db_path,
            data_dir=data_dir,
            cache_dir=cache_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
            admin_ttl_hours=0.0,
            auth_mode="hosted",
        )
        assert summary_keep["jobs"] == 0
        assert db.get_job(db_path, "admin_keep") is not None

        remaining_projects = {p.project_id for p in projects_store.list_projects(db_path)}
        assert remaining_projects == {p_guest_fresh, p_friend_fresh, p_admin_old}
        assert not old_cache.exists()
        assert new_cache.is_file()

    def test_worker_main_none_mode_preserves_legacy_max_age_hours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "none")
        monkeypatch.delenv("GRIDFORGE_GUEST_TTL_HOURS", raising=False)
        monkeypatch.delenv("GRIDFORGE_FRIEND_TTL_HOURS", raising=False)

        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        cache_dir = tmp_path / "cache"
        data_dir.mkdir()
        packs_dir.mkdir()
        cache_dir.mkdir()
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)

        _seed_job(db_path, job_id="local_old", owner_id="local", owner_role="none", status="succeeded", age_hours=2.5, data_dir=data_dir)
        _seed_job(db_path, job_id="local_fresh", owner_id="local", owner_role="none", status="succeeded", age_hours=0.2, data_dir=data_dir)

        rc = worker.main(
            [
                "--data-dir",
                str(data_dir),
                "--packs-dir",
                str(packs_dir),
                "--cache-dir",
                str(cache_dir),
                "--max-age-hours",
                "1.0",
                "--once",
            ]
        )
        assert rc == 0
        assert db.get_job(db_path, "local_old") is None
        assert not (data_dir / "local_old").exists()
        assert db.get_job(db_path, "local_fresh") is not None

    def test_run_worker_loop_runs_on_startup_and_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        cache_dir = tmp_path / "cache"
        data_dir.mkdir()
        packs_dir.mkdir()
        cache_dir.mkdir()

        prune_calls: list[float] = []
        monotonic_values = iter([1.0, 905.0, 910.0])

        def _fake_monotonic() -> float:
            try:
                return next(monotonic_values)
            except StopIteration:
                return 910.0

        monkeypatch.setattr(worker.time, "monotonic", _fake_monotonic)

        class _CountingEvent:
            def __init__(self) -> None:
                self.waits = 0

            def is_set(self) -> bool:
                return self.waits >= 2

            def set(self) -> None:
                self.waits = 99

            def wait(self, timeout: float = 0.0) -> bool:
                self.waits += 1
                return self.is_set()

        monkeypatch.setattr(threading, "Event", _CountingEvent)

        def _fake_prune(*args: object, **kwargs: object) -> dict[str, int]:
            prune_calls.append(1.0)
            return {"jobs": 0, "projects": 0, "sessions": 0, "invites": 0}

        monkeypatch.setattr(worker, "run_pruning_pass", _fake_prune)

        worker.run_worker_loop(
            data_dir=data_dir,
            packs_dir=packs_dir,
            cache_dir=cache_dir,
            prune_interval_seconds=900.0,
            poll_seconds=0.01,
            once=False,
        )
        # First call at t=1.0 (startup despite t < 900), second call at t=905.0.
        assert len(prune_calls) == 2

    def test_run_worker_loop_pruning_exception_does_not_crash_job_processing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        cache_dir = tmp_path / "cache"
        data_dir.mkdir()
        packs_dir.mkdir()
        cache_dir.mkdir()
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)

        _seed_job(db_path, job_id="queued_job_1", owner_id="local", owner_role="none", status="queued", age_hours=0.1)

        def _boom(*args: object, **kwargs: object) -> cache_prune.CachePruneResult:
            raise RuntimeError("disk hiccup")

        processed: list[str] = []

        def _fake_process(claimed: db.ClaimedJob, **kwargs: object) -> None:
            processed.append(claimed.record.job_id)

        monkeypatch.setattr(cache_prune, "trim_cache_dir", _boom)
        monkeypatch.setattr(worker, "process_single_job", _fake_process)

        worker.run_worker_loop(
            data_dir=data_dir,
            packs_dir=packs_dir,
            cache_dir=cache_dir,
            cache_max_bytes=100,
            once=True,
        )
        assert processed == ["queued_job_1"]

    def test_worker_main_cache_max_bytes_none_disables_trimming(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        cache_dir = tmp_path / "cache"
        data_dir.mkdir()
        packs_dir.mkdir()
        cache_dir.mkdir()
        cached = cache_dir / "big.tif"
        cached.write_bytes(b"x" * 256)

        rc = worker.main(
            [
                "--data-dir",
                str(data_dir),
                "--packs-dir",
                str(packs_dir),
                "--cache-dir",
                str(cache_dir),
                "--cache-max-bytes",
                "none",
                "--once",
            ]
        )
        assert rc == 0
        assert cached.is_file()

    def test_none_mode_with_compose_role_ttls_still_prunes_local_jobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "none")
        monkeypatch.setenv("GRIDFORGE_GUEST_TTL_HOURS", "24")
        monkeypatch.setenv("GRIDFORGE_FRIEND_TTL_HOURS", "336")

        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        cache_dir = tmp_path / "cache"
        data_dir.mkdir()
        packs_dir.mkdir()
        cache_dir.mkdir()
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)

        _seed_job(db_path, job_id="local_48h_pass", owner_id="local", owner_role="none", status="succeeded", age_hours=48.0, data_dir=data_dir)
        _seed_job(db_path, job_id="local_fresh_pass", owner_id="local", owner_role="none", status="succeeded", age_hours=0.2, data_dir=data_dir)

        summary = worker.run_pruning_pass(
            db_path,
            data_dir=data_dir,
            cache_dir=cache_dir,
            max_job_age_hours=1.0,
        )
        assert summary["jobs"] == 1
        assert db.get_job(db_path, "local_48h_pass") is None
        assert not (data_dir / "local_48h_pass").exists()
        assert db.get_job(db_path, "local_fresh_pass") is not None

        _seed_job(db_path, job_id="local_48h_cli", owner_id="local", owner_role="none", status="succeeded", age_hours=48.0, data_dir=data_dir)
        rc = worker.main(
            [
                "--data-dir",
                str(data_dir),
                "--packs-dir",
                str(packs_dir),
                "--cache-dir",
                str(cache_dir),
                "--max-age-hours",
                "1.0",
                "--once",
            ]
        )
        assert rc == 0
        assert db.get_job(db_path, "local_48h_cli") is None
        assert not (data_dir / "local_48h_cli").exists()
        assert db.get_job(db_path, "local_fresh_pass") is not None

    def test_worker_main_empty_env_and_empty_cache_dir_do_not_touch_cwd_or_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_cwd = tmp_path / "fake_cwd"
        fake_cwd.mkdir()
        keep_file = fake_cwd / "source_file.py"
        keep_file.write_text("print('keep')\n", encoding="utf-8")
        empty_subdir = fake_cwd / "important_empty_dir"
        empty_subdir.mkdir()
        monkeypatch.chdir(fake_cwd)

        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        isolated_default_cache = tmp_path / "default_cache"
        data_dir.mkdir()
        packs_dir.mkdir()
        isolated_default_cache.mkdir()

        monkeypatch.setattr(worker, "DEFAULT_CACHE_DIR", str(isolated_default_cache))
        monkeypatch.setenv("GRIDFORGE_CACHE_DIR", "")
        monkeypatch.setenv("GRIDFORGE_JOB_TTL_HOURS", "")
        monkeypatch.setenv("GRIDFORGE_WORKER_ID", "   ")
        monkeypatch.setenv("GRIDFORGE_CACHE_MAX_BYTES", "0")

        # 1. Empty env GRIDFORGE_CACHE_DIR="" + GRIDFORGE_JOB_TTL_HOURS="" must not crash or touch $PWD
        rc = worker.main(
            [
                "--data-dir",
                str(data_dir),
                "--packs-dir",
                str(packs_dir),
                "--once",
            ]
        )
        assert rc == 0
        assert keep_file.is_file()
        assert empty_subdir.is_dir()

        # 2. Explicit --cache-dir "" or whitespace must never resolve to $PWD
        for blank_arg in ("", "   "):
            rc_blank = worker.main(
                [
                    "--data-dir",
                    str(data_dir),
                    "--packs-dir",
                    str(packs_dir),
                    "--cache-dir",
                    blank_arg,
                    "--cache-max-bytes",
                    "0",
                    "--once",
                ]
            )
            assert rc_blank == 0
            assert keep_file.is_file()
            assert empty_subdir.is_dir()

        # 3. Empty GRIDFORGE_DATA_DIR / GRIDFORGE_PACKS_DIR / GRIDFORGE_CACHE_DIR / GRIDFORGE_WORKER_ID / GRIDFORGE_JOB_TTL_HOURS fallback cleanly
        captured: dict[str, object] = {}

        def _fake_loop(**kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(worker, "run_worker_loop", _fake_loop)
        monkeypatch.setenv("GRIDFORGE_DATA_DIR", "  ")
        monkeypatch.setenv("GRIDFORGE_PACKS_DIR", "")
        monkeypatch.setenv("GRIDFORGE_CACHE_DIR", "")
        monkeypatch.setenv("GRIDFORGE_WORKER_ID", "")
        monkeypatch.setenv("GRIDFORGE_JOB_TTL_HOURS", " ")
        assert worker.main(["--once"]) == 0
        assert captured["data_dir"] == Path("/data/jobs").resolve()
        assert captured["packs_dir"] == Path("/data/packs").resolve()
        assert captured["cache_dir"] == isolated_default_cache.resolve()
        assert captured["worker_id"] == "worker-1"
        assert captured["max_job_age_hours"] == 24.0

    def test_t5_delete_running_job_does_not_orphan_artifacts_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gridforge.web.schemas import CreateJobRequest

        data_dir = tmp_path / "data"
        packs_dir = tmp_path / "packs"
        data_dir.mkdir()
        packs_dir.mkdir()
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)

        req = CreateJobRequest(center="-35.3,149.1", scale=50000)
        db.create_job(
            db_path,
            job_id="joborphan01",
            title="Orphan",
            center="-35.3,149.1",
            center_mgrs="55HFA",
            scale=50000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=req.model_dump_json(),
            owner_id="guest:g1",
            owner_role="guest",
        )
        claimed = db.claim_next_job(db_path)
        assert claimed is not None
        entered, release = threading.Event(), threading.Event()

        class FakeWS:
            dem_utm_tif = tmp_path / "none.tif"
            layout = None
            declination = None

        class FakeRenderer:
            def render_pdf(self, ws, pdf_path: Path) -> None:
                entered.set()
                release.wait(5.0)
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                pdf_path.write_bytes(b"%PDF-1.4\n")

        monkeypatch.setattr("gridforge.pipeline.runner.prepare_job_workspace", lambda *a, **kw: FakeWS())
        monkeypatch.setattr("gridforge.web.worker.PyQGISRenderer", lambda: FakeRenderer())

        t = threading.Thread(
            target=worker.process_single_job,
            args=(claimed,),
            kwargs={"db_path": db_path, "data_dir": data_dir, "packs_dir": packs_dir},
        )
        t.start()
        assert entered.wait(5.0)
        assert db.delete_job(db_path, "joborphan01", data_dir=data_dir) is True
        release.set()
        t.join(5.0)

        assert not (data_dir / "joborphan01").exists()

        # Also verify db.prune_expired_jobs sweeps any stranded orphan dirs past grace window
        stranded = data_dir / "jobstranded02" / "artifacts"
        stranded.mkdir(parents=True)
        (stranded / "stale.pdf").write_bytes(b"%PDF-1.4\n")
        old_ts = 1_000_000.0
        os.utime(data_dir / "jobstranded02", (old_ts, old_ts))
        db.prune_expired_jobs(
            db_path,
            data_dir=data_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
        )
        assert not (data_dir / "jobstranded02").exists()

    def test_m2_orphan_grace_window_uses_wall_clock_and_skips_tmp_upload_staging(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)

        fresh_upload = data_dir / "freshupload01" / "uploads"
        fresh_upload.mkdir(parents=True)
        (fresh_upload / "track.geojson").write_text("{}", encoding="utf-8")

        tmp_staging = data_dir / ".tmp_upload_abc12345" / "uploads"
        tmp_staging.mkdir(parents=True)
        (tmp_staging / "part.geojson").write_text("{}", encoding="utf-8")

        # Even when a synthetic `now` 25h in the future is passed for DB TTL pruning,
        # the orphan grace window must still compare against real wall-clock time.time().
        synthetic_future = datetime.now(timezone.utc) + timedelta(hours=25)
        db.prune_expired_jobs(
            db_path,
            data_dir=data_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
            orphan_grace_seconds=60.0,
            now=synthetic_future,
        )
        assert (data_dir / "freshupload01").exists()
        assert (data_dir / ".tmp_upload_abc12345").exists()
