"""Unit tests for single-container entrypoint supervision and deployment configs."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from gridforge.web import entrypoint

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeProcess:
    def __init__(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        poll_sequence: list[int | None] | None = None,
        wait_raises_timeout: bool = False,
        exit_after_signal: int = 0,
    ) -> None:
        self.cmd = cmd
        self.env = env or {}
        self.pid = 1000 + len(cmd)
        self._poll_sequence = list(poll_sequence) if poll_sequence is not None else []
        self._wait_raises_timeout = wait_raises_timeout
        self._exit_after_signal = exit_after_signal
        self._returncode: int | None = None
        self.signals_received: list[int] = []
        self.killed = False
        self.terminated = False

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if self._poll_sequence:
            nxt = self._poll_sequence.pop(0)
            if nxt is not None:
                self._returncode = nxt
            return nxt
        return None

    def send_signal(self, sig: int) -> None:
        self.signals_received.append(sig)
        if not self._wait_raises_timeout:
            self._returncode = self._exit_after_signal

    def terminate(self) -> None:
        self.terminated = True
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.killed = True
        self._returncode = -signal.SIGKILL

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_raises_timeout and not self.killed:
            raise subprocess.TimeoutExpired(self.cmd, timeout or 0.0)
        if self._returncode is None:
            self._returncode = 0
        return self._returncode


class TestEntrypointConfigAndSupervision:
    def test_zero_qgis_or_pyqt5_imports(self) -> None:
        assert not any(
            mod.startswith(("qgis", "PyQt5")) for mod in sys.modules
        ), "gridforge.web.entrypoint must never import qgis or PyQt5"

    def test_configure_environment_creates_dirs_and_preserves_overrides(self, tmp_path: Path) -> None:
        vol_root = tmp_path / "vol"
        env, host, port = entrypoint.configure_environment(
            {},
            volume_root=vol_root,
            seed_samples_dir=tmp_path / "missing-samples",
        )
        assert host == "0.0.0.0"
        assert port == 8080
        assert env["GRIDFORGE_DATA_DIR"] == str((vol_root / "jobs").resolve())
        assert env["GRIDFORGE_PACKS_DIR"] == str((vol_root / "packs").resolve())
        assert env["GRIDFORGE_CACHE_DIR"] == str((vol_root / "cache").resolve())
        assert env["GRIDFORGE_SAMPLES_DIR"] == str((vol_root / "samples").resolve())
        for sub in ("jobs", "packs", "cache", "samples"):
            assert (vol_root / sub).is_dir()

        custom_data = tmp_path / "custom_jobs"
        env_custom, _, _ = entrypoint.configure_environment(
            {"GRIDFORGE_DATA_DIR": str(custom_data)},
            volume_root=vol_root,
            seed_samples_dir=tmp_path / "missing-samples",
        )
        assert env_custom["GRIDFORGE_DATA_DIR"] == str(custom_data)
        assert custom_data.is_dir()

    def test_configure_environment_forwarded_allow_ips_defaults(self, tmp_path: Path) -> None:
        env_none, _, _ = entrypoint.configure_environment(
            {"GRIDFORGE_AUTH_MODE": "none"},
            volume_root=tmp_path,
            seed_samples_dir=tmp_path / "none",
        )
        assert env_none["FORWARDED_ALLOW_IPS"] == "127.0.0.1"

        env_hosted, _, _ = entrypoint.configure_environment(
            {"GRIDFORGE_AUTH_MODE": "hosted"},
            volume_root=tmp_path,
            seed_samples_dir=tmp_path / "none",
        )
        assert env_hosted["FORWARDED_ALLOW_IPS"] == "*"

        env_explicit, _, _ = entrypoint.configure_environment(
            {"GRIDFORGE_AUTH_MODE": "hosted", "FORWARDED_ALLOW_IPS": "10.0.0.1"},
            volume_root=tmp_path,
            seed_samples_dir=tmp_path / "none",
        )
        assert env_explicit["FORWARDED_ALLOW_IPS"] == "10.0.0.1"

    def test_configure_environment_railway_allowed_hosts_injected_and_deduped(
        self, tmp_path: Path
    ) -> None:
        env_local, _, _ = entrypoint.configure_environment(
            {"GRIDFORGE_ALLOWED_HOSTS": "maps.example.com"},
            volume_root=tmp_path,
            seed_samples_dir=tmp_path / "none",
        )
        assert env_local["GRIDFORGE_ALLOWED_HOSTS"] == "maps.example.com"

        env_railway, _, _ = entrypoint.configure_environment(
            {
                "RAILWAY_ENVIRONMENT_ID": "env-123",
                "RAILWAY_PUBLIC_DOMAIN": "maps.example.com",
                "GRIDFORGE_ALLOWED_HOSTS": "maps.example.com, healthcheck.railway.app",
            },
            volume_root=tmp_path,
            seed_samples_dir=tmp_path / "none",
        )
        hosts = env_railway["GRIDFORGE_ALLOWED_HOSTS"].split(",")
        assert hosts == ["maps.example.com", "healthcheck.railway.app"]

    def test_port_validation_and_build_commands(self, tmp_path: Path) -> None:
        _, host, port = entrypoint.configure_environment(
            {"GRIDFORGE_HOST": "127.0.0.1", "PORT": "9090"},
            volume_root=tmp_path,
            seed_samples_dir=tmp_path / "none",
        )
        assert host == "127.0.0.1"
        assert port == 9090
        web_cmd, worker_cmd = entrypoint.build_commands(host, port, python_executable="/opt/venv/bin/python")
        assert web_cmd == [
            "/opt/venv/bin/python",
            "-m",
            "uvicorn",
            "gridforge.web.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "9090",
            "--proxy-headers",
        ]
        assert worker_cmd == ["/opt/venv/bin/python", "-m", "gridforge.web.worker"]

        for bad_port in ("abc", "0", "65536", "-5"):
            with pytest.raises(ValueError):
                entrypoint.configure_environment(
                    {"PORT": bad_port},
                    volume_root=tmp_path,
                    seed_samples_dir=tmp_path / "none",
                )

    def test_seeds_missing_sample_pdfs_without_overwriting_existing(self, tmp_path: Path) -> None:
        seed_dir = tmp_path / "baked_samples"
        seed_dir.mkdir()
        (seed_dir / "alpha.pdf").write_bytes(b"new-alpha")
        (seed_dir / "beta.pdf").write_bytes(b"new-beta")
        (seed_dir / "notes.txt").write_text("ignore", encoding="utf-8")

        vol_root = tmp_path / "vol"
        samples_dir = vol_root / "samples"
        samples_dir.mkdir(parents=True)
        (samples_dir / "alpha.pdf").write_bytes(b"existing-alpha")

        entrypoint.configure_environment(
            {},
            volume_root=vol_root,
            seed_samples_dir=seed_dir,
        )
        assert (samples_dir / "alpha.pdf").read_bytes() == b"existing-alpha"
        assert (samples_dir / "beta.pdf").read_bytes() == b"new-beta"
        assert not (samples_dir / "notes.txt").exists()

    def test_supervise_inits_db_and_exits_nonzero_on_child_crash(self, tmp_path: Path) -> None:
        inited_dbs: list[Path] = []
        spawned: list[_FakeProcess] = []

        def _fake_init_db(db_path: Path) -> None:
            inited_dbs.append(db_path)

        def _fake_popen(cmd: list[str], env: dict[str, str] | None = None) -> _FakeProcess:
            if "uvicorn" in cmd:
                proc = _FakeProcess(cmd, env=env, poll_sequence=[None, None])
            else:
                # Worker crashes immediately on second poll (or first poll) with rc=2
                proc = _FakeProcess(cmd, env=env, poll_sequence=[2])
            spawned.append(proc)
            return proc

        rc = entrypoint.supervise(
            env={},
            volume_root=tmp_path / "vol",
            seed_samples_dir=tmp_path / "none",
            poll_interval=0.001,
            shutdown_grace_seconds=0.1,
            popen_cls=_fake_popen,
            init_db_fn=_fake_init_db,
        )
        assert rc == 2
        assert len(inited_dbs) == 1
        assert inited_dbs[0] == (tmp_path / "vol" / "jobs" / "gridforge_jobs.sqlite3").resolve()
        assert len(spawned) == 2
        assert spawned[0].signals_received == [signal.SIGTERM]

    def test_supervise_unexpected_zero_exit_still_returns_nonzero(self, tmp_path: Path) -> None:
        spawned: list[_FakeProcess] = []

        def _fake_popen(cmd: list[str], env: dict[str, str] | None = None) -> _FakeProcess:
            poll_seq = [0] if "uvicorn" in cmd else [None]
            proc = _FakeProcess(cmd, env=env, poll_sequence=poll_seq)
            spawned.append(proc)
            return proc

        rc = entrypoint.supervise(
            env={},
            volume_root=tmp_path / "vol",
            seed_samples_dir=tmp_path / "none",
            poll_interval=0.001,
            shutdown_grace_seconds=0.1,
            popen_cls=_fake_popen,
            init_db_fn=lambda _p: None,
        )
        assert rc == 1
        assert spawned[1].signals_received == [signal.SIGTERM]

    def test_supervise_cleans_up_web_if_worker_spawn_fails(self, tmp_path: Path) -> None:
        spawned: list[_FakeProcess] = []

        def _fake_popen(cmd: list[str], env: dict[str, str] | None = None) -> _FakeProcess:
            if "uvicorn" in cmd:
                proc = _FakeProcess(cmd, env=env)
                spawned.append(proc)
                return proc
            raise OSError("spawn failed")

        with pytest.raises(OSError, match="spawn failed"):
            entrypoint.supervise(
                env={},
                volume_root=tmp_path / "vol",
                seed_samples_dir=tmp_path / "none",
                popen_cls=_fake_popen,
                init_db_fn=lambda _p: None,
            )
        assert len(spawned) == 1
        assert spawned[0].signals_received == [signal.SIGTERM]

    def test_supervise_forwards_sigterm_and_escalates_to_sigkill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered_handlers: dict[int, Any] = {}
        orig_signal = signal.signal

        def _capture_signal(signum: int, handler: Any) -> Any:
            registered_handlers[signum] = handler
            return orig_signal(signum, handler)

        monkeypatch.setattr(entrypoint.signal, "signal", _capture_signal)

        spawned: list[_FakeProcess] = []

        def _fake_popen(cmd: list[str], env: dict[str, str] | None = None) -> _FakeProcess:
            # Worker ignores SIGTERM and requires SIGKILL; web exits cleanly.
            is_worker = "gridforge.web.worker" in cmd
            proc = _FakeProcess(cmd, env=env, wait_raises_timeout=is_worker)
            spawned.append(proc)
            return proc

        def _trigger_sigterm(_seconds: float) -> None:
            handler = registered_handlers[signal.SIGTERM]
            handler(signal.SIGTERM, None)

        monkeypatch.setattr(entrypoint.time, "sleep", _trigger_sigterm)

        rc = entrypoint.supervise(
            env={},
            volume_root=tmp_path / "vol",
            seed_samples_dir=tmp_path / "none",
            poll_interval=0.01,
            shutdown_grace_seconds=0.05,
            popen_cls=_fake_popen,
            init_db_fn=lambda _p: None,
        )
        # Web exited cleanly (0); worker escalated to SIGKILL (-9) outside clean rcs or non-zero
        assert spawned[0].signals_received == [signal.SIGTERM]
        assert spawned[1].signals_received == [signal.SIGTERM]
        assert spawned[1].killed is True
        assert rc == -signal.SIGKILL

    def test_supervise_signals_both_children_before_shared_grace_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered_handlers: dict[int, Any] = {}
        orig_signal = signal.signal

        def _capture_signal(signum: int, handler: Any) -> Any:
            registered_handlers[signum] = handler
            return orig_signal(signum, handler)

        monkeypatch.setattr(entrypoint.signal, "signal", _capture_signal)

        events: list[tuple[str, str, float | int | None]] = []
        fake_now = [100.0]

        def _fake_monotonic() -> float:
            return fake_now[0]

        monkeypatch.setattr(entrypoint.time, "monotonic", _fake_monotonic)

        class _StubbornRecordingProcess(_FakeProcess):
            def __init__(self, role: str, cmd: list[str], env: dict[str, str] | None = None) -> None:
                super().__init__(cmd, env=env, wait_raises_timeout=True)
                self.role = role

            def send_signal(self, sig: int) -> None:
                events.append(("signal", self.role, sig))
                super().send_signal(sig)

            def wait(self, timeout: float | None = None) -> int:
                events.append(("wait", self.role, timeout))
                if not self.killed:
                    # Simulate first child consuming the full remaining grace window
                    fake_now[0] += float(timeout or 0.0)
                    raise subprocess.TimeoutExpired(self.cmd, timeout or 0.0)
                return super().wait(timeout=timeout)

            def kill(self) -> None:
                events.append(("kill", self.role, None))
                super().kill()

        spawned: list[_StubbornRecordingProcess] = []

        def _fake_popen(cmd: list[str], env: dict[str, str] | None = None) -> _StubbornRecordingProcess:
            role = "worker" if "gridforge.web.worker" in cmd else "web"
            proc = _StubbornRecordingProcess(role, cmd, env=env)
            spawned.append(proc)
            return proc

        def _trigger_sigterm(_seconds: float) -> None:
            handler = registered_handlers[signal.SIGTERM]
            handler(signal.SIGTERM, None)

        monkeypatch.setattr(entrypoint.time, "sleep", _trigger_sigterm)

        rc = entrypoint.supervise(
            env={},
            volume_root=tmp_path / "vol",
            seed_samples_dir=tmp_path / "none",
            poll_interval=0.01,
            shutdown_grace_seconds=2.5,
            popen_cls=_fake_popen,
            init_db_fn=lambda _p: None,
        )
        assert rc == -signal.SIGKILL
        # Both children are signaled BEFORE waiting or killing either child.
        assert events[0] == ("signal", "web", signal.SIGTERM)
        assert events[1] == ("signal", "worker", signal.SIGTERM)
        # Shared grace deadline: web waits 2.5s; worker sees 0.0s remaining (not another 2.5s).
        assert events[2] == ("wait", "web", 2.5)
        assert events[3] == ("wait", "worker", 0.0)
        assert ("kill", "web", None) in events
        assert ("kill", "worker", None) in events
        assert fake_now[0] == pytest.approx(102.5)


class TestDeploymentConfigs:
    def test_dockerfile_single_container_and_cmd_preserved(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "ENTRYPOINT" not in dockerfile
        assert "GRIDFORGE_CACHE_DIR=/tmp/gridforge-cache" not in dockerfile
        assert "GRIDFORGE_DATA_DIR=/data/jobs" not in dockerfile
        assert "GRIDFORGE_PACKS_DIR=/data/packs" not in dockerfile
        assert "EXPOSE 8080" in dockerfile
        assert "HEALTHCHECK" in dockerfile
        assert "/api/health" in dockerfile
        assert 'CMD ["python3", "-m", "gridforge.web.entrypoint"]' in dockerfile

    def test_railway_toml_valid_schema_and_no_volumes_key(self) -> None:
        raw = (REPO_ROOT / "railway.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(raw)
        assert "volumes" not in parsed
        assert parsed["build"]["builder"] == "DOCKERFILE"
        assert parsed["build"]["dockerfilePath"] == "Dockerfile"
        assert parsed["deploy"]["startCommand"] == "python3 -m gridforge.web.entrypoint"
        assert parsed["deploy"]["healthcheckPath"] == "/api/health"
        assert parsed["deploy"]["restartPolicyType"] == "ON_FAILURE"

    @pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.ghcr.yml"])
    def test_compose_files_preserve_local_defaults_and_passthroughs(self, filename: str) -> None:
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        assert "127.0.0.1:" in text
        assert "--proxy-headers" in text
        assert "GRIDFORGE_CACHE_DIR=/tmp/gridforge-cache" in text
        assert "GRIDFORGE_AUTH_MODE=${GRIDFORGE_AUTH_MODE:-none}" in text
        assert "GRIDFORGE_PRUNE_INTERVAL_SECONDS=${GRIDFORGE_PRUNE_INTERVAL_SECONDS:-900}" in text
        assert "GRIDFORGE_GUEST_TTL_HOURS=${GRIDFORGE_GUEST_TTL_HOURS:-24}" in text
        assert "GRIDFORGE_FRIEND_TTL_HOURS=${GRIDFORGE_FRIEND_TTL_HOURS:-336}" in text
        assert "GRIDFORGE_CACHE_MAX_BYTES=${GRIDFORGE_CACHE_MAX_BYTES:-2147483648}" in text
        for svc in ("web:", "worker:", "studio:", "cli:"):
            assert svc in text
