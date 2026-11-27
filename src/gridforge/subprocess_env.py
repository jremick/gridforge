"""Allowlisted subprocess environment builder isolating secrets from child processes.

Prevents web-tier credentials (session signing secrets, admin passwords, OAuth
secrets, Turnstile secrets, origin proxy secrets) from leaking into worker,
PyQGIS, PyQt5 supplement, or GDAL/OGR/Osmium subprocesses.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_EXPLICIT_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "GRIDFORGE_SESSION_SECRET",
        "GRIDFORGE_ADMIN_PASSWORD",
        "GRIDFORGE_ADMIN_EMAIL",
        "GRIDFORGE_GOOGLE_CLIENT_SECRET",
        "GRIDFORGE_GOOGLE_CLIENT_ID",
        "GRIDFORGE_TURNSTILE_SECRET_KEY",
        "GRIDFORGE_TURNSTILE_SITE_KEY",
        "GRIDFORGE_ORIGIN_PROXY_SECRET",
        "GRIDFORGE_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "RAILWAY_API_TOKEN",
        "RAILWAY_TOKEN",
    }
)

_ALLOWED_EXACT_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "DISPLAY",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "FONTCONFIG_PATH",
        "FONTCONFIG_FILE",
        "OVERPASS_URL",
        "OVERPASS_FALLBACK_URLS",
        "GRIDFORGE_DATA_DIR",
        "GRIDFORGE_PACKS_DIR",
        "GRIDFORGE_CACHE_DIR",
        "GRIDFORGE_SAMPLES_DIR",
        "GRIDFORGE_VOLUME_ROOT",
        "GRIDFORGE_AUTH_MODE",
        "GRIDFORGE_LOG_LEVEL",
        "GRIDFORGE_MEMORY_BUDGET_MB",
        "GRIDFORGE_PRUNE_INTERVAL_SECONDS",
        "GRIDFORGE_GUEST_TTL_HOURS",
        "GRIDFORGE_FRIEND_TTL_HOURS",
        "GRIDFORGE_ADMIN_TTL_HOURS",
        "GRIDFORGE_JOB_TTL_HOURS",
        "GRIDFORGE_CACHE_MAX_BYTES",
        "GRIDFORGE_BRAND_NAME",
        "GRIDFORGE_BRAND_SUBTITLE",
        "GRIDFORGE_BRAND_TAGLINE",
        "GRIDFORGE_USE_XVFB",
        "GRIDFORGE_RUN_LIVE",
        "GRIDFORGE_MAX_GUEST_PROJECTS",
    }
)

_ALLOWED_PREFIXES: tuple[str, ...] = (
    "LC_",
    "QT_",
    "QGIS_",
    "PROJ_",
    "GDAL_",
    "OGR_",
    "VSI_",
    "CPL_",
    "OSM_",
    "OMP_",
    "OPENBLAS_",
    "GRIDFORGE_WORKER_",
)


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    if upper in _EXPLICIT_SECRET_KEYS:
        return True
    if upper.startswith("GRIDFORGE_") and any(
        tok in upper for tok in ("SECRET", "PASSWORD", "TOKEN", "CLIENT_SECRET", "API_KEY")
    ):
        return True
    return False


def build_safe_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
    include_linz_key: bool = False,
    allow_network: bool = True,
    private_runtime_dir: Path | None = None,
) -> dict[str, str]:
    """Return an allowlisted environment dict stripped of web/application secrets."""
    source = os.environ if base is None else base
    env: dict[str, str] = {}

    for key, val in source.items():
        if _is_secret_key(key):
            continue
        if key == "LINZ_API_KEY":
            if include_linz_key:
                env[key] = val
            continue
        if key in _ALLOWED_EXACT_KEYS or key.startswith(_ALLOWED_PREFIXES):
            env[key] = val

    if allow_network:
        env.setdefault("GDAL_HTTP_TIMEOUT", "30")
        env.setdefault("GDAL_HTTP_CONNECTTIMEOUT", "10")
    else:
        env["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = ".nonexistent"
        env["GDAL_HTTP_MAX_RETRY"] = "0"
        env["GDAL_HTTP_TIMEOUT"] = "1"
        env["GDAL_HTTP_CONNECTTIMEOUT"] = "1"

    if private_runtime_dir is not None:
        resolved_rt = private_runtime_dir.resolve()
        resolved_rt.mkdir(parents=True, exist_ok=True)
        try:
            resolved_rt.chmod(0o700)
        except OSError:
            pass
        env["XDG_RUNTIME_DIR"] = str(resolved_rt)

    if extra:
        for key, val in extra.items():
            if not _is_secret_key(key):
                env[key] = val

    return env
