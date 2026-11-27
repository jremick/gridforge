"""Per-role capabilities, request caps, and SQLite-backed rate/quota enforcement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from gridforge.web import db
from gridforge.web.schemas import (
    AuthCapabilities,
    AuthQuotas,
    CreateJobRequest,
)

ALL_OUTPUT_KEYS: tuple[str, ...] = (
    "pdf",
    "geopdf",
    "mbtiles",
    "kmz",
    "gpkg_tiles",
    "osmand_sqlitedb",
    "field_bundle_zip",
)

_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})
_FALSY: frozenset[str] = frozenset({"0", "false", "no"})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    norm = raw.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _env_opt_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    norm = raw.strip().lower()
    if norm in {"none", "unlimited", "off"}:
        return None
    val = int(norm)
    return None if val < 0 else val


def _env_outputs(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    items = [part.strip().lower() for part in raw.split(",") if part.strip()]
    filtered = tuple(item for item in items if item in ALL_OUTPUT_KEYS)
    return filtered or default


def _env_upload_bytes(specific_name: str, default_bytes: int) -> int:
    raw_spec = os.environ.get(specific_name)
    if raw_spec is not None and raw_spec.strip():
        return max(1024, int(raw_spec.strip()))
    raw_global = os.environ.get("GRIDFORGE_MAX_UPLOAD_BYTES")
    if raw_global is not None and raw_global.strip():
        return max(1024, int(raw_global.strip()))
    return default_bytes


@dataclass(frozen=True)
class LimitsConfig:
    """Environment-configured per-role limits and global hosted budgets."""

    auth_mode: str = "none"
    guest_max_grid_cells: int = 1
    friend_max_grid_cells: int = 9
    guest_strip_allowed: bool = False
    friend_strip_allowed: bool = True
    guest_max_strip_sheets: int = 0
    friend_max_strip_sheets: int = 12
    guest_allowed_outputs: tuple[str, ...] = ("pdf",)
    friend_allowed_outputs: tuple[str, ...] = ALL_OUTPUT_KEYS
    guest_max_upload_bytes: int = 5 * 1024 * 1024
    friend_max_upload_bytes: int = 25 * 1024 * 1024
    admin_max_upload_bytes: int = 50 * 1024 * 1024
    none_max_upload_bytes: int = 25 * 1024 * 1024
    guest_max_dpi: int = 300
    friend_max_dpi: int = 600
    admin_max_dpi: int = 600
    none_max_dpi: int = 600
    guest_daily_renders: int | None = 2
    friend_daily_renders: int | None = 20
    guest_hourly_previews: int | None = 5
    friend_hourly_previews: int | None = 60
    guest_rf_link_hourly_limit: int | None = 30
    friend_rf_link_hourly_limit: int | None = 120
    guest_share_hourly_limit: int | None = 30
    friend_share_hourly_limit: int | None = 120
    guest_share_ttl_hours: float = 168.0
    friend_share_ttl_hours: float = 720.0
    geocode_hourly_limit: int | None = 30
    auth_rate_limit_per_minute: int | None = 20
    max_queue_depth: int | None = 10
    global_daily_public_renders: int | None = 50

    @classmethod
    def from_env(cls, *, auth_mode: str = "none") -> LimitsConfig:
        is_hosted = auth_mode == "hosted"
        return cls(
            auth_mode=auth_mode,
            guest_max_grid_cells=max(1, min(9, _env_int("GRIDFORGE_GUEST_MAX_GRID_CELLS", 1))),
            friend_max_grid_cells=max(1, min(9, _env_int("GRIDFORGE_FRIEND_MAX_GRID_CELLS", 9))),
            guest_strip_allowed=_env_bool("GRIDFORGE_GUEST_STRIP_ALLOWED", False),
            friend_strip_allowed=_env_bool("GRIDFORGE_FRIEND_STRIP_ALLOWED", True),
            guest_max_strip_sheets=max(0, min(24, _env_int("GRIDFORGE_GUEST_MAX_STRIP_SHEETS", 0))),
            friend_max_strip_sheets=max(1, min(24, _env_int("GRIDFORGE_FRIEND_MAX_STRIP_SHEETS", 12))),
            guest_allowed_outputs=_env_outputs("GRIDFORGE_GUEST_ALLOWED_OUTPUTS", ("pdf",)),
            friend_allowed_outputs=_env_outputs("GRIDFORGE_FRIEND_ALLOWED_OUTPUTS", ALL_OUTPUT_KEYS),
            guest_max_upload_bytes=_env_upload_bytes("GRIDFORGE_MAX_UPLOAD_BYTES_GUEST", 5 * 1024 * 1024),
            friend_max_upload_bytes=_env_upload_bytes("GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND", 25 * 1024 * 1024),
            admin_max_upload_bytes=_env_upload_bytes("GRIDFORGE_MAX_UPLOAD_BYTES_ADMIN", 50 * 1024 * 1024),
            none_max_upload_bytes=_env_upload_bytes("GRIDFORGE_MAX_UPLOAD_BYTES", 25 * 1024 * 1024),
            guest_max_dpi=max(72, min(600, _env_int("GRIDFORGE_GUEST_MAX_DPI", 300))),
            friend_max_dpi=max(72, min(600, _env_int("GRIDFORGE_FRIEND_MAX_DPI", 600))),
            admin_max_dpi=max(72, min(600, _env_int("GRIDFORGE_ADMIN_MAX_DPI", 600))),
            none_max_dpi=max(72, min(600, _env_int("GRIDFORGE_MAX_DPI", 600))),
            guest_daily_renders=_env_opt_int("GRIDFORGE_GUEST_DAILY_RENDERS", 2 if is_hosted else None),
            friend_daily_renders=_env_opt_int("GRIDFORGE_FRIEND_DAILY_RENDERS", 20 if is_hosted else None),
            guest_hourly_previews=_env_opt_int("GRIDFORGE_GUEST_HOURLY_PREVIEWS", 5 if is_hosted else None),
            friend_hourly_previews=_env_opt_int("GRIDFORGE_FRIEND_HOURLY_PREVIEWS", 60 if is_hosted else None),
            guest_rf_link_hourly_limit=_env_opt_int(
                "GRIDFORGE_RF_LINK_HOURLY_LIMIT", 30 if is_hosted else None
            ),
            friend_rf_link_hourly_limit=_env_opt_int(
                "GRIDFORGE_FRIEND_RF_LINK_HOURLY_LIMIT", 120 if is_hosted else None
            ),
            guest_share_hourly_limit=_env_opt_int(
                "GRIDFORGE_SHARE_HOURLY_LIMIT", 30 if is_hosted else None
            ),
            friend_share_hourly_limit=_env_opt_int(
                "GRIDFORGE_FRIEND_SHARE_HOURLY_LIMIT", 120 if is_hosted else None
            ),
            guest_share_ttl_hours=max(
                1.0, float(_env_int("GRIDFORGE_GUEST_SHARE_TTL_HOURS", 168))
            ),
            friend_share_ttl_hours=max(
                1.0, float(_env_int("GRIDFORGE_FRIEND_SHARE_TTL_HOURS", 720))
            ),
            geocode_hourly_limit=_env_opt_int(
                "GRIDFORGE_GEOCODE_HOURLY_LIMIT", 30 if is_hosted else None
            ),
            auth_rate_limit_per_minute=_env_opt_int(
                "GRIDFORGE_AUTH_RATE_LIMIT_PER_MINUTE", 20 if is_hosted else None
            ),
            max_queue_depth=_env_opt_int("GRIDFORGE_MAX_QUEUE_DEPTH", 10 if is_hosted else None),
            global_daily_public_renders=_env_opt_int(
                "GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS", 50 if is_hosted else None
            ),
        )


def _grid_dims_for_cells(max_cells: int) -> tuple[int, int]:
    if max_cells <= 1:
        return (1, 1)
    dim = min(3, max_cells)
    return (dim, dim)


def get_capabilities(
    role: str,
    cfg: LimitsConfig,
    *,
    turnstile_sitekey: str | None = None,
    turnstile_configured: bool = False,
    google_oauth_enabled: bool = False,
) -> AuthCapabilities:
    """Resolve effective ``AuthCapabilities`` for a role."""
    if cfg.auth_mode == "none" or role == "none":
        return AuthCapabilities(
            max_grid_cells=9,
            max_grid_cols=3,
            max_grid_rows=3,
            strip_atlas_allowed=True,
            max_strip_sheets=24,
            allowed_outputs=list(ALL_OUTPUT_KEYS),
            packs_manage_allowed=True,
            max_upload_bytes=cfg.none_max_upload_bytes,
            max_dpi=cfg.none_max_dpi,
            max_export_dpi=cfg.none_max_dpi,
            turnstile_required=False,
            turnstile_sitekey=None,
            google_oauth_enabled=False,
        )

    if role == "admin":
        return AuthCapabilities(
            max_grid_cells=9,
            max_grid_cols=3,
            max_grid_rows=3,
            strip_atlas_allowed=True,
            max_strip_sheets=24,
            allowed_outputs=list(ALL_OUTPUT_KEYS),
            packs_manage_allowed=True,
            max_upload_bytes=cfg.admin_max_upload_bytes,
            max_dpi=cfg.admin_max_dpi,
            max_export_dpi=cfg.admin_max_dpi,
            turnstile_required=False,
            turnstile_sitekey=turnstile_sitekey,
            google_oauth_enabled=google_oauth_enabled,
        )

    if role == "friend":
        f_cols, f_rows = _grid_dims_for_cells(cfg.friend_max_grid_cells)
        return AuthCapabilities(
            max_grid_cells=cfg.friend_max_grid_cells,
            max_grid_cols=f_cols,
            max_grid_rows=f_rows,
            strip_atlas_allowed=cfg.friend_strip_allowed,
            max_strip_sheets=cfg.friend_max_strip_sheets if cfg.friend_strip_allowed else 0,
            allowed_outputs=list(cfg.friend_allowed_outputs),
            packs_manage_allowed=False,
            max_upload_bytes=cfg.friend_max_upload_bytes,
            max_dpi=cfg.friend_max_dpi,
            max_export_dpi=cfg.friend_max_dpi,
            turnstile_required=False,
            turnstile_sitekey=turnstile_sitekey,
            google_oauth_enabled=google_oauth_enabled,
        )

    # Guest role
    g_cols, g_rows = _grid_dims_for_cells(cfg.guest_max_grid_cells)
    return AuthCapabilities(
        max_grid_cells=cfg.guest_max_grid_cells,
        max_grid_cols=g_cols,
        max_grid_rows=g_rows,
        strip_atlas_allowed=cfg.guest_strip_allowed,
        max_strip_sheets=cfg.guest_max_strip_sheets if cfg.guest_strip_allowed else 0,
        allowed_outputs=list(cfg.guest_allowed_outputs),
        packs_manage_allowed=False,
        max_upload_bytes=cfg.guest_max_upload_bytes,
        max_dpi=cfg.guest_max_dpi,
        max_export_dpi=cfg.guest_max_dpi,
        turnstile_required=turnstile_configured,
        turnstile_sitekey=turnstile_sitekey if turnstile_configured else None,
        google_oauth_enabled=google_oauth_enabled,
    )


def effective_daily_render_limit(
    role: str,
    cfg: LimitsConfig,
    user_dict: dict[str, Any] | None,
) -> int | None:
    if cfg.auth_mode != "hosted" or role in {"none", "admin"}:
        return None
    if role == "friend":
        if user_dict is not None and user_dict.get("daily_render_limit") is not None:
            return int(user_dict["daily_render_limit"])
        return cfg.friend_daily_renders
    return cfg.guest_daily_renders


def effective_hourly_preview_limit(
    role: str,
    cfg: LimitsConfig,
    user_dict: dict[str, Any] | None,
) -> int | None:
    if cfg.auth_mode != "hosted" or role in {"none", "admin"}:
        return None
    if role == "friend":
        if user_dict is not None and user_dict.get("hourly_preview_limit") is not None:
            return int(user_dict["hourly_preview_limit"])
        return cfg.friend_hourly_previews
    return cfg.guest_hourly_previews


def effective_rf_link_hourly_limit(role: str, cfg: LimitsConfig) -> int | None:
    if cfg.auth_mode != "hosted" or role in {"none", "admin"}:
        return None
    if role == "friend":
        return cfg.friend_rf_link_hourly_limit
    return cfg.guest_rf_link_hourly_limit


def effective_geocode_hourly_limit(role: str, cfg: LimitsConfig) -> int | None:
    if cfg.auth_mode != "hosted" or role in {"none", "admin"}:
        return None
    return cfg.geocode_hourly_limit


def effective_share_hourly_limit(role: str, cfg: LimitsConfig) -> int | None:
    if cfg.auth_mode != "hosted" or role in {"none", "admin"}:
        return None
    if role == "friend":
        return cfg.friend_share_hourly_limit
    return cfg.guest_share_hourly_limit


def effective_share_ttl_hours(
    role: str,
    cfg: LimitsConfig,
    requested_hours: int | None = None,
) -> float | None:
    """Resolve snapshot TTL in hours, capping guest/friend durations in hosted mode."""
    if cfg.auth_mode != "hosted" or role in {"none", "admin"}:
        return float(requested_hours) if requested_hours is not None else None
    max_ttl = cfg.friend_share_ttl_hours if role == "friend" else cfg.guest_share_ttl_hours
    if requested_hours is None:
        return max_ttl
    return float(min(max_ttl, max(1.0, float(requested_hours))))


def get_quotas_snapshot(
    db_path: Path,
    *,
    role: str,
    owner_id: str,
    client_ip: str,
    user_dict: dict[str, Any] | None,
    cfg: LimitsConfig,
    now: datetime | None = None,
) -> AuthQuotas:
    """Compute live quota usage and remaining counts for ``GET /api/auth/session``."""
    if cfg.auth_mode != "hosted":
        return AuthQuotas()

    ref_now = db._resolve_now(now)
    today_start_iso = db._dt_to_iso(ref_now.replace(hour=0, minute=0, second=0, microsecond=0))
    hour_ago_iso = db._dt_to_iso(ref_now - timedelta(hours=1))

    daily_limit = effective_daily_render_limit(role, cfg, user_dict)
    hourly_limit = effective_hourly_preview_limit(role, cfg, user_dict)

    daily_used = db.count_usage_events(
        db_path,
        event_type="job_render",
        since_iso=today_start_iso,
        actor_id=owner_id,
    )
    hourly_used = db.count_usage_events(
        db_path,
        event_type=("viewshed_preview", "ruler_metrics"),
        since_iso=hour_ago_iso,
        actor_id=owner_id,
    )
    if role == "guest" and client_ip:
        daily_ip = db.count_usage_events(
            db_path,
            event_type="job_render",
            since_iso=today_start_iso,
            actor_role="guest",
            client_ip=client_ip,
        )
        hourly_ip = db.count_usage_events(
            db_path,
            event_type=("viewshed_preview", "ruler_metrics"),
            since_iso=hour_ago_iso,
            actor_role="guest",
            client_ip=client_ip,
        )
        daily_used = max(daily_used, daily_ip)
        hourly_used = max(hourly_used, hourly_ip)

    global_public_used = db.count_usage_events(
        db_path,
        event_type="job_render",
        since_iso=today_start_iso,
        actor_role="guest",
    )
    public_paused = db.get_bool_setting(db_path, "public_renders_paused", default=False)

    return AuthQuotas(
        daily_renders_limit=daily_limit,
        daily_renders_used=daily_used,
        daily_renders_remaining=(
            max(0, daily_limit - daily_used) if daily_limit is not None else None
        ),
        hourly_previews_limit=hourly_limit,
        hourly_previews_used=hourly_used,
        hourly_previews_remaining=(
            max(0, hourly_limit - hourly_used) if hourly_limit is not None else None
        ),
        public_renders_paused=public_paused,
        global_daily_public_limit=cfg.global_daily_public_renders,
        global_daily_public_used=global_public_used,
    )


def validate_job_capabilities(
    role: str,
    job_req: CreateJobRequest,
    cap: AuthCapabilities,
) -> None:
    """Enforce per-role grid size, strip atlas, and output format caps (`403`)."""
    requested_cells = int(job_req.grid_cols) * int(job_req.grid_rows)
    if requested_cells > cap.max_grid_cells:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Grid size {job_req.grid_cols}x{job_req.grid_rows} exceeds your account limit "
                f"({cap.max_grid_cells} sheet{'s' if cap.max_grid_cells != 1 else ''})."
            ),
        )

    if job_req.strip_sheets:
        if not cap.strip_atlas_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Strip atlas rendering requires an invited Friend or Admin account.",
            )
        if len(job_req.strip_sheets) > cap.max_strip_sheets:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Strip atlas has {len(job_req.strip_sheets)} sheets, exceeding your "
                    f"account limit of {cap.max_strip_sheets}."
                ),
            )

    allowed = set(cap.allowed_outputs)
    for out_key in ALL_OUTPUT_KEYS:
        if bool(getattr(job_req.outputs, out_key, False)) and out_key not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Output format '{out_key}' is not enabled for role '{role}'. "
                    f"Allowed outputs: {', '.join(cap.allowed_outputs)}."
                ),
            )

    if int(job_req.dpi) > cap.max_dpi:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Print resolution {job_req.dpi} DPI exceeds your account limit ({cap.max_dpi} DPI). "
                "Sign in with an invited account for up to 600 DPI."
            ),
        )

    if job_req.outputs.export_dpi is not None and int(job_req.outputs.export_dpi) > cap.max_export_dpi:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Digital export resolution {job_req.outputs.export_dpi} DPI exceeds your account limit "
                f"({cap.max_export_dpi} DPI). Sign in with an invited account for up to 600 DPI."
            ),
        )


def enforce_pre_upload_job_limits(
    db_path: Path,
    *,
    role: str,
    owner_id: str,
    client_ip: str,
    user_dict: dict[str, Any] | None,
    cfg: LimitsConfig,
) -> None:
    """Fail fast before streaming uploads when kill-switch, queue, budget, or daily quota is exceeded."""
    if cfg.auth_mode != "hosted":
        if cfg.max_queue_depth is not None and db.get_queue_depth(db_path) >= cfg.max_queue_depth:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Render queue is currently full; please try again shortly.",
            )
        return

    quotas = get_quotas_snapshot(
        db_path,
        role=role,
        owner_id=owner_id,
        client_ip=client_ip,
        user_dict=user_dict,
        cfg=cfg,
    )
    if role == "guest" and quotas.public_renders_paused:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Public map rendering is temporarily paused by the administrator.",
        )
    if cfg.max_queue_depth is not None and db.get_queue_depth(db_path) >= cfg.max_queue_depth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Render queue is currently full; please try again shortly.",
        )
    if (
        role == "guest"
        and cfg.global_daily_public_renders is not None
        and quotas.global_daily_public_used >= cfg.global_daily_public_renders
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Global daily public render budget reached; sign in or try again tomorrow UTC.",
        )
    if (
        quotas.daily_renders_limit is not None
        and quotas.daily_renders_used >= quotas.daily_renders_limit
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily render quota ({quotas.daily_renders_limit}/day) reached.",
        )


def enforce_and_create_job(
    db_path: Path,
    *,
    role: str,
    owner_id: str,
    client_ip: str,
    user_dict: dict[str, Any] | None,
    cfg: LimitsConfig,
    **create_kwargs: Any,
) -> Any:
    """Atomically verify limits and insert job + usage event inside SQLite ``BEGIN IMMEDIATE``."""
    is_hosted = cfg.auth_mode == "hosted"
    daily_limit = effective_daily_render_limit(role, cfg, user_dict) if is_hosted else None
    global_limit = cfg.global_daily_public_renders if is_hosted else None
    try:
        return db.atomic_check_and_create_job(
            db_path,
            owner_id=owner_id,
            owner_role=role,
            client_ip=client_ip,
            max_queue_depth=cfg.max_queue_depth,
            global_daily_public_limit=global_limit,
            daily_render_limit=daily_limit,
            enforce_kill_switch=is_hosted,
            record_usage=is_hosted,
            **create_kwargs,
        )
    except db.QuotaLimitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def enforce_and_record_preview(
    db_path: Path,
    *,
    role: str,
    owner_id: str,
    client_ip: str,
    user_dict: dict[str, Any] | None,
    cfg: LimitsConfig,
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Enforce hourly rate limit and record ``viewshed_preview``, ``rf_link_profile``, or ``ruler_metrics``."""
    if cfg.auth_mode != "hosted":
        return

    count_event_types: tuple[str, ...] | None = None
    if event_type == "viewshed_preview":
        limit = effective_hourly_preview_limit(role, cfg, user_dict)
        count_event_types = ("viewshed_preview", "ruler_metrics")
    elif event_type == "rf_link_profile":
        limit = effective_rf_link_hourly_limit(role, cfg)
    elif event_type == "ruler_metrics":
        limit = effective_hourly_preview_limit(role, cfg, user_dict)
        count_event_types = ("viewshed_preview", "ruler_metrics")
    else:
        limit = None

    try:
        db.atomic_check_and_record_rate_limit(
            db_path,
            event_type=event_type,
            actor_id=owner_id,
            actor_role=role,
            client_ip=client_ip,
            limit=limit,
            window_seconds=3600,
            by_ip_only=False,
            count_event_types=count_event_types,
            detail=detail,
        )
    except db.QuotaLimitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def enforce_and_record_geocode_miss(
    db_path: Path,
    *,
    role: str,
    owner_id: str,
    client_ip: str,
    cfg: LimitsConfig,
    query: str,
) -> None:
    """Enforce per-IP hourly geocode cache-miss limit and record ``geocode_miss``."""
    if cfg.auth_mode != "hosted":
        return
    limit = effective_geocode_hourly_limit(role, cfg)
    try:
        db.atomic_check_and_record_rate_limit(
            db_path,
            event_type="geocode_miss",
            actor_id=owner_id,
            actor_role=role,
            client_ip=client_ip,
            limit=limit,
            window_seconds=3600,
            by_ip_only=True,
            detail={"q": query[:80]},
        )
    except db.QuotaLimitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def enforce_and_record_share_create(
    db_path: Path,
    *,
    role: str,
    owner_id: str,
    client_ip: str,
    cfg: LimitsConfig,
) -> None:
    """Enforce hourly snapshot creation limit in hosted mode and record ``share_create``."""
    if cfg.auth_mode != "hosted":
        return
    limit = effective_share_hourly_limit(role, cfg)
    try:
        db.atomic_check_and_record_rate_limit(
            db_path,
            event_type="share_create",
            actor_id=owner_id,
            actor_role=role,
            client_ip=client_ip,
            limit=limit,
            window_seconds=3600,
            by_ip_only=(role == "guest"),
        )
    except db.QuotaLimitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def enforce_and_record_auth_attempt(
    db_path: Path,
    *,
    event_type: str,
    client_ip: str,
    cfg: LimitsConfig,
    detail: dict[str, Any] | None = None,
) -> None:
    """Enforce per-IP 1-minute rolling rate limit on password login and invite redemption."""
    if cfg.auth_mode != "hosted":
        return
    limit = cfg.auth_rate_limit_per_minute
    try:
        db.atomic_check_and_record_rate_limit(
            db_path,
            event_type=event_type,
            actor_id=f"ip:{client_ip}",
            actor_role="auth",
            client_ip=client_ip,
            limit=limit,
            window_seconds=60,
            by_ip_only=True,
            detail=detail,
        )
    except db.QuotaLimitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
