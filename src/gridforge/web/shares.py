"""SQLite-backed Shareable Map Snapshot store for GridForge Studio."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from gridforge.web import db

_SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")

MAX_SHARE_STATE_BYTES = 65_536
DEFAULT_GUEST_SHARE_TTL_HOURS = 168.0  # 7 days
DEFAULT_FRIEND_SHARE_TTL_HOURS = 720.0  # 30 days

ALLOWED_SHARE_STATE_KEYS: frozenset[str] = frozenset(
    {
        "center",
        "lat",
        "lon",
        "title",
        "scale",
        "paper",
        "orientation",
        "color_mode",
        "units",
        "dpi",
        "grid_cols",
        "grid_rows",
        "dem_source",
        "pack_id",
        "basemap",
        "map_zoom",
        "au_overlays",
        "nz_overlays",
        "ca_overlays",
        "us_overlays",
        "carto",
        "outputs",
        "waypoints",
        "viewsheds",
        "rf_links",
        "tactical_graphics",
        "include_calibration_page",
        "include_route_card",
        "include_elevation_profile",
        "include_atlas_index",
        "include_rf_link_card",
        "show_fold_marks",
    }
)

_LIST_LIMITS: dict[str, int] = {
    "au_overlays": 16,
    "nz_overlays": 8,
    "ca_overlays": 8,
    "us_overlays": 16,
    "waypoints": 200,
    "viewsheds": 10,
    "rf_links": 20,
    "tactical_graphics": 50,
}


class SharePayloadTooLargeError(ValueError):
    """Raised when serialized snapshot state exceeds ``MAX_SHARE_STATE_BYTES``."""


class ShareCreateRequest(BaseModel):
    """Request payload for ``POST /api/shares``."""

    name: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=500)
    state: dict[str, Any] | None = None
    project_id: str | None = Field(default=None, max_length=64)
    expires_in_hours: int | None = Field(default=None, ge=1, le=8760)

    @model_validator(mode="after")
    def _require_state_or_project(self) -> ShareCreateRequest:
        has_project = bool(self.project_id and self.project_id.strip())
        has_state = isinstance(self.state, dict) and len(self.state) > 0
        if not has_project and not has_state:
            raise ValueError("Either project_id or a non-empty state payload is required.")
        if self.project_id is not None:
            cleaned = self.project_id.strip()
            self.project_id = cleaned or None
        self.name = self.name.strip()
        self.notes = self.notes.strip()
        return self


class ShareSnapshotPublic(BaseModel):
    """Read-only public view of a shared map snapshot (strips tenant/owner metadata)."""

    share_id: str
    share_path: str
    name: str = ""
    notes: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    view_count: int = 0
    created_at: str
    expires_at: str | None = None


class ShareSnapshotSummary(BaseModel):
    """Owner-visible summary row returned by ``GET /api/shares``."""

    share_id: str
    share_path: str
    name: str = ""
    notes: str = ""
    source_project_id: str | None = None
    view_count: int = 0
    created_at: str
    expires_at: str | None = None


class SharesListResponse(BaseModel):
    """Response envelope for ``GET /api/shares``."""

    shares: list[ShareSnapshotSummary] = Field(default_factory=list)


def is_valid_share_id(share_id: str) -> bool:
    """Return True if ``share_id`` matches the allowed URL-safe token syntax."""
    return bool(_SHARE_ID_RE.match(share_id))


def sanitize_share_state(state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Allowlist keys, cap collection sizes, and enforce ``MAX_SHARE_STATE_BYTES``."""
    if not isinstance(state, dict):
        raise ValueError("Share state must be a JSON object.")

    cleaned: dict[str, Any] = {}
    for key, value in state.items():
        if key not in ALLOWED_SHARE_STATE_KEYS:
            continue
        if key in _LIST_LIMITS:
            if not isinstance(value, list):
                continue
            cleaned[key] = value[: _LIST_LIMITS[key]]
        elif key in {"carto", "outputs"}:
            if isinstance(value, dict):
                cleaned[key] = value
        else:
            cleaned[key] = value

    if not cleaned:
        raise ValueError("Share state did not contain any valid map configuration keys.")

    try:
        serialized = json.dumps(cleaned, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid share state payload: {exc}") from exc

    if len(serialized.encode("utf-8")) > MAX_SHARE_STATE_BYTES:
        raise SharePayloadTooLargeError(
            f"Share snapshot exceeds maximum size of {MAX_SHARE_STATE_BYTES // 1024} KB."
        )

    return cleaned, serialized


def _is_expired(expires_at: str | None, now_iso: str) -> bool:
    return bool(expires_at and expires_at <= now_iso)


def _row_to_public(row: sqlite3.Row) -> ShareSnapshotPublic:
    sid = str(row["share_id"])
    raw_state = row["state_json"]
    try:
        state = json.loads(raw_state) if raw_state else {}
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    return ShareSnapshotPublic(
        share_id=sid,
        share_path=f"/?share={sid}",
        name=str(row["name"] or ""),
        notes=str(row["notes"] or ""),
        state=state,
        view_count=int(row["view_count"] or 0),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]) if row["expires_at"] else None,
    )


def _row_to_summary(row: sqlite3.Row) -> ShareSnapshotSummary:
    sid = str(row["share_id"])
    return ShareSnapshotSummary(
        share_id=sid,
        share_path=f"/?share={sid}",
        name=str(row["name"] or ""),
        notes=str(row["notes"] or ""),
        source_project_id=(
            str(row["source_project_id"]) if row["source_project_id"] else None
        ),
        view_count=int(row["view_count"] or 0),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]) if row["expires_at"] else None,
    )


def create_share(
    db_path: Path,
    *,
    name: str,
    notes: str,
    state: dict[str, Any],
    source_project_id: str | None = None,
    owner_id: str = "local",
    owner_role: str = "none",
    ttl_hours: float | None = None,
    now: datetime | None = None,
) -> ShareSnapshotPublic:
    """Persist an immutable shareable snapshot and return its public record."""
    _cleaned, state_json = sanitize_share_state(state)
    ref_now = db._resolve_now(now)
    now_iso = db._dt_to_iso(ref_now)
    expires_at: str | None = (
        db._dt_to_iso(ref_now + timedelta(hours=ttl_hours))
        if ttl_hours is not None
        else None
    )

    clean_name = name.strip()[:120]
    clean_notes = notes.strip()[:500]
    conn = db._connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        share_id = ""
        for _ in range(5):
            candidate = secrets.token_urlsafe(12)
            exists = conn.execute(
                "SELECT 1 FROM shared_snapshots WHERE share_id = ?",
                (candidate,),
            ).fetchone()
            if exists is None:
                share_id = candidate
                break
        if not share_id:
            raise RuntimeError("Failed to allocate unique share_id.")

        conn.execute(
            """
            INSERT INTO shared_snapshots (
                share_id, name, notes, state_json, source_project_id,
                owner_id, owner_role, view_count, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                share_id,
                clean_name,
                clean_notes,
                state_json,
                source_project_id,
                owner_id,
                owner_role,
                now_iso,
                expires_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM shared_snapshots WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_public(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_public_share(
    db_path: Path,
    share_id: str,
    *,
    increment_views: bool = True,
    now: datetime | None = None,
) -> ShareSnapshotPublic | None:
    """Fetch a public shareable snapshot by ID, returning ``None`` if missing or expired."""
    now_iso = db._dt_to_iso(db._resolve_now(now))
    conn = db._connect(db_path)
    try:
        if increment_views:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM shared_snapshots WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        if row is None or _is_expired(
            str(row["expires_at"]) if row["expires_at"] else None, now_iso
        ):
            if increment_views:
                conn.execute("COMMIT")
            return None

        if increment_views:
            conn.execute(
                "UPDATE shared_snapshots SET view_count = view_count + 1 WHERE share_id = ?",
                (share_id,),
            )
            row = conn.execute(
                "SELECT * FROM shared_snapshots WHERE share_id = ?",
                (share_id,),
            ).fetchone()
            conn.execute("COMMIT")
            assert row is not None
        return _row_to_public(row)
    except Exception:
        if increment_views:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_share_owner(db_path: Path, share_id: str) -> tuple[str, str] | None:
    """Return ``(owner_id, owner_role)`` for a share ID, or ``None`` if not found."""
    conn = db._connect(db_path)
    try:
        row = conn.execute(
            "SELECT owner_id, owner_role FROM shared_snapshots WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        if row is None:
            return None
        return (str(row["owner_id"]), str(row["owner_role"]))
    finally:
        conn.close()


def list_shares(
    db_path: Path,
    *,
    limit: int = 100,
    owner_id: str | None = None,
    now: datetime | None = None,
) -> list[ShareSnapshotSummary]:
    """List non-expired shareable snapshots ordered newest first."""
    now_iso = db._dt_to_iso(db._resolve_now(now))
    clamped = max(1, min(500, limit))
    conn = db._connect(db_path)
    try:
        if owner_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM shared_snapshots
                WHERE owner_id = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (owner_id, now_iso, clamped),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM shared_snapshots
                WHERE (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (now_iso, clamped),
            ).fetchall()
        return [_row_to_summary(r) for r in rows]
    finally:
        conn.close()


def delete_share(db_path: Path, share_id: str) -> bool:
    """Revoke and delete a shareable snapshot by ID."""
    conn = db._connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM shared_snapshots WHERE share_id = ?",
            (share_id,),
        )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
