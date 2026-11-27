"""Single-container supervisor entrypoint for GridForge Web + Worker.

Runs ``uvicorn gridforge.web.app:app`` and ``python3 -m gridforge.web.worker``
side by side against a shared persistent volume (default ``/data``) with clean
signal propagation and fail-fast child monitoring.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from gridforge.subprocess_env import build_safe_subprocess_env
from gridforge.web import db

logger = logging.getLogger(__name__)

DEFAULT_HOSTED_FORWARDED_ALLOW_IPS: str = (
    "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1,fc00::/7"
)

_RAILWAY_ENV_MARKERS: tuple[str, ...] = (
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_PROJECT_ID",
)


def _setdefault_nonempty(env: dict[str, str], key: str, default: str) -> str:
    current = env.get(key, "").strip()
    if not current:
        env[key] = default
        return default
    return current


def _append_allowed_hosts(existing: str, additions: list[str]) -> str:
    seen_lower: set[str] = set()
    hosts: list[str] = []
    for raw in [*existing.split(","), *additions]:
        host = raw.strip()
        if not host:
            continue
        key = host.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            hosts.append(host)
    return ",".join(hosts)


def _seed_sample_pdfs(seed_dir: Path, samples_dir: Path) -> int:
    if not seed_dir.is_dir():
        return 0
    if seed_dir.resolve() == samples_dir.resolve():
        return 0
    copied = 0
    for src in sorted(seed_dir.iterdir()):
        if src.is_file() and src.suffix.lower() == ".pdf":
            dst = samples_dir / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
                copied += 1
    return copied


def configure_environment(
    env: Mapping[str, str] | None = None,
    *,
    volume_root: Path | str | None = None,
    seed_samples_dir: Path | str | None = None,
) -> tuple[dict[str, str], str, int]:
    """Prepare directories, environment defaults, proxy/Railway settings, and bind target."""
    configured = dict(os.environ if env is None else env)

    raw_root = (
        str(volume_root)
        if volume_root is not None
        else (configured.get("GRIDFORGE_VOLUME_ROOT") or "/data")
    )
    resolved_root = Path(raw_root).resolve()

    data_dir = Path(_setdefault_nonempty(configured, "GRIDFORGE_DATA_DIR", str(resolved_root / "jobs"))).resolve()
    packs_dir = Path(_setdefault_nonempty(configured, "GRIDFORGE_PACKS_DIR", str(resolved_root / "packs"))).resolve()
    cache_dir = Path(_setdefault_nonempty(configured, "GRIDFORGE_CACHE_DIR", str(resolved_root / "cache"))).resolve()
    samples_dir = Path(
        _setdefault_nonempty(configured, "GRIDFORGE_SAMPLES_DIR", str(resolved_root / "samples"))
    ).resolve()

    for directory in (data_dir, packs_dir, cache_dir, samples_dir):
        directory.mkdir(parents=True, exist_ok=True)

    seed_dir = Path(seed_samples_dir) if seed_samples_dir is not None else Path("/app/samples")
    _seed_sample_pdfs(seed_dir, samples_dir)

    auth_mode = (configured.get("GRIDFORGE_AUTH_MODE") or "none").strip().lower() or "none"
    if not configured.get("FORWARDED_ALLOW_IPS", "").strip():
        configured["FORWARDED_ALLOW_IPS"] = (
            DEFAULT_HOSTED_FORWARDED_ALLOW_IPS if auth_mode == "hosted" else "127.0.0.1"
        )

    if any(configured.get(marker, "").strip() for marker in _RAILWAY_ENV_MARKERS):
        existing_hosts = configured.get("GRIDFORGE_ALLOWED_HOSTS", "").strip()
        extra_hosts = ["healthcheck.railway.app"]
        public_domain = configured.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        # Only auto-trust RAILWAY_PUBLIC_DOMAIN when the operator has not explicitly
        # restricted GRIDFORGE_ALLOWED_HOSTS to custom domains.
        if public_domain and not existing_hosts:
            extra_hosts.append(public_domain)
        configured["GRIDFORGE_ALLOWED_HOSTS"] = _append_allowed_hosts(
            existing_hosts,
            extra_hosts,
        )

    host = (configured.get("GRIDFORGE_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    raw_port = (configured.get("PORT") or "8080").strip() or "8080"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"Invalid PORT={raw_port!r}: expected integer in 1..65535") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"Invalid PORT={port}: must be between 1 and 65535")

    return configured, host, port


def build_commands(
    host: str,
    port: int,
    *,
    python_executable: str | None = None,
) -> tuple[list[str], list[str]]:
    """Build exact argv lists for the web server and worker processes."""
    py = python_executable or sys.executable
    web_cmd = [
        py,
        "-m",
        "uvicorn",
        "gridforge.web.app:app",
        "--host",
        host,
        "--port",
        str(port),
        "--proxy-headers",
    ]
    worker_cmd = [py, "-m", "gridforge.web.worker"]
    return web_cmd, worker_cmd


def _terminate_processes(procs: list[Any], *, sig: int, grace_seconds: float) -> list[int | None]:
    """Signal all live processes first, then wait up to one shared grace deadline before SIGKILL."""
    results: list[int | None] = [None] * len(procs)
    live_indices: list[int] = []

    for idx, proc in enumerate(procs):
        if proc is None:
            continue
        rc = proc.poll()
        if rc is not None:
            results[idx] = int(rc)
            continue
        try:
            if hasattr(proc, "send_signal"):
                proc.send_signal(sig)
            else:
                proc.terminate()
        except ProcessLookupError:
            rc = proc.poll()
            results[idx] = int(rc) if rc is not None else None
            continue
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        live_indices.append(idx)

    grace = max(0.0, grace_seconds)
    deadline = time.monotonic() + grace
    timed_out_indices: list[int] = []

    for idx in live_indices:
        proc = procs[idx]
        rc = proc.poll()
        if rc is not None:
            results[idx] = int(rc)
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            results[idx] = int(proc.wait(timeout=remaining))
        except subprocess.TimeoutExpired:
            timed_out_indices.append(idx)

    for idx in timed_out_indices:
        proc = procs[idx]
        rc = proc.poll()
        if rc is not None:
            results[idx] = int(rc)
            continue
        logger.warning(
            "Child process pid=%s did not exit within %.1fs; sending SIGKILL",
            getattr(proc, "pid", "?"),
            grace,
        )
        try:
            proc.kill()
        except OSError:
            pass
        try:
            results[idx] = int(proc.wait(timeout=5.0))
        except subprocess.TimeoutExpired:
            rc = proc.poll()
            results[idx] = int(rc) if rc is not None else None

    return results


def _terminate_process(proc: Any, *, sig: int, grace_seconds: float) -> int | None:
    return _terminate_processes([proc], sig=sig, grace_seconds=grace_seconds)[0]


def _resolve_drop_privileges(
    configured_env: dict[str, str],
    *,
    popen_cls: Any,
) -> dict[str, Any]:
    """If started as root (e.g. Railway volume mount), chown dirs and drop to ``gridforge``."""
    if popen_cls is not subprocess.Popen or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return {}
    try:
        import pwd

        pw = pwd.getpwnam("gridforge")
    except (ImportError, KeyError):
        return {}

    uid, gid = pw.pw_uid, pw.pw_gid
    dirs_to_own = [
        Path(configured_env["GRIDFORGE_DATA_DIR"]),
        Path(configured_env["GRIDFORGE_PACKS_DIR"]),
        Path(configured_env["GRIDFORGE_CACHE_DIR"]),
        Path(configured_env["GRIDFORGE_SAMPLES_DIR"]),
    ]
    rt_dir = Path(configured_env.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-gridforge"))
    rt_dir.mkdir(parents=True, exist_ok=True)
    dirs_to_own.append(rt_dir)

    for target in dirs_to_own:
        try:
            os.chown(target, uid, gid)
            for root_dir, subdirs, files in os.walk(target):
                for name in subdirs:
                    try:
                        os.chown(os.path.join(root_dir, name), uid, gid)
                    except OSError:
                        pass
                for name in files:
                    try:
                        os.chown(os.path.join(root_dir, name), uid, gid)
                    except OSError:
                        pass
        except OSError as exc:
            logger.warning("Could not chown %s for gridforge user: %s", target, exc)

    try:
        rt_dir.chmod(0o700)
    except OSError:
        pass
    configured_env["HOME"] = pw.pw_dir
    return {"user": uid, "group": gid}


def supervise(
    *,
    env: Mapping[str, str] | None = None,
    volume_root: Path | str | None = None,
    seed_samples_dir: Path | str | None = None,
    poll_interval: float = 0.25,
    shutdown_grace_seconds: float | None = None,
    popen_cls: Any = subprocess.Popen,
    init_db_fn: Callable[[Path], None] = db.init_db,
) -> int:
    """Initialize shared volume/DB and supervise web + worker processes until exit."""
    configured_env, host, port = configure_environment(
        env,
        volume_root=volume_root,
        seed_samples_dir=seed_samples_dir,
    )
    data_dir = Path(configured_env["GRIDFORGE_DATA_DIR"])
    init_db_fn(data_dir / "gridforge_jobs.sqlite3")
    drop_kwargs = _resolve_drop_privileges(configured_env, popen_cls=popen_cls)
    worker_env = build_safe_subprocess_env(base=configured_env, include_linz_key=True)

    if shutdown_grace_seconds is not None:
        grace = max(0.0, float(shutdown_grace_seconds))
    else:
        raw_grace = configured_env.get("GRIDFORGE_SHUTDOWN_GRACE_SECONDS", "10.0").strip() or "10.0"
        grace = max(0.0, float(raw_grace))

    web_cmd, worker_cmd = build_commands(host, port)
    stop_signal: int | None = None

    def _on_signal(signum: int, _frame: Any) -> None:
        nonlocal stop_signal
        stop_signal = signum

    on_main_thread = threading.current_thread() is threading.main_thread()
    prev_term = prev_int = None
    if on_main_thread:
        prev_term = signal.getsignal(signal.SIGTERM)
        prev_int = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    web_proc: Any = None
    worker_proc: Any = None
    try:
        logger.info(
            "Starting GridForge single-container supervisor (host=%s, port=%d, data_dir=%s, cache_dir=%s)",
            host,
            port,
            configured_env["GRIDFORGE_DATA_DIR"],
            configured_env["GRIDFORGE_CACHE_DIR"],
        )
        web_proc = popen_cls(web_cmd, env=configured_env, **drop_kwargs)
        try:
            worker_proc = popen_cls(worker_cmd, env=worker_env, **drop_kwargs)
        except Exception:
            _terminate_process(web_proc, sig=signal.SIGTERM, grace_seconds=grace)
            raise

        while True:
            if stop_signal is not None:
                logger.info("Received signal %d; stopping web and worker processes...", stop_signal)
                web_rc, worker_rc = _terminate_processes(
                    [web_proc, worker_proc],
                    sig=stop_signal,
                    grace_seconds=grace,
                )
                clean_rcs = {0, -stop_signal, 128 + stop_signal}
                for rc in (web_rc, worker_rc):
                    if rc is not None and rc not in clean_rcs:
                        return int(rc)
                return 0

            web_rc = web_proc.poll()
            worker_rc = worker_proc.poll()
            if web_rc is not None or worker_rc is not None:
                if web_rc is not None:
                    logger.error("Web process exited unexpectedly with code %s; terminating worker.", web_rc)
                    _terminate_process(worker_proc, sig=signal.SIGTERM, grace_seconds=grace)
                    return int(web_rc) if int(web_rc) != 0 else 1
                assert worker_rc is not None
                logger.error("Worker process exited unexpectedly with code %s; terminating web.", worker_rc)
                _terminate_process(web_proc, sig=signal.SIGTERM, grace_seconds=grace)
                return int(worker_rc) if int(worker_rc) != 0 else 1

            time.sleep(poll_interval)
    finally:
        if on_main_thread:
            if prev_term is not None:
                signal.signal(signal.SIGTERM, prev_term)
            if prev_int is not None:
                signal.signal(signal.SIGINT, prev_int)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("GRIDFORGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="GridForge single-container entrypoint supervising web API and worker"
    )
    parser.add_argument("--volume-root", type=Path, default=None)
    parser.add_argument("--seed-samples-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    return supervise(volume_root=args.volume_root, seed_samples_dir=args.seed_samples_dir)


if __name__ == "__main__":
    raise SystemExit(main())
