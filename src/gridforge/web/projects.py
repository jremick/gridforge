"""SQLite-backed Saved Trip Projects CRUD store for GridForge Studio."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from gridforge.web import db

_PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
MAX_PROJECT_STATE_BYTES = 65_536
DEFAULT_MAX_PROJECTS_PER_GUEST = 5
DEFAULT_MAX_GUEST_PROJECTS = 500
DEFAULT_GUEST_PROJECT_TTL_HOURS = 24.0


class ProjectPayloadTooLargeError(ValueError):
    """Raised when serialized project state exceeds ``MAX_PROJECT_STATE_BYTES``."""


def _serialize_project_state(state: dict[str, Any]) -> str:
    try:
        serialized = json.dumps(state, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid project state payload: {exc}") from exc
    if len(serialized.encode("utf-8")) > MAX_PROJECT_STATE_BYTES:
        raise ProjectPayloadTooLargeError(
            f"Project state exceeds maximum size of {MAX_PROJECT_STATE_BYTES // 1024} KB."
        )
    return serialized


def _env_pos_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _env_pos_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


class ProjectCreateRequest(BaseModel):
    """Request payload for ``POST /api/projects``."""

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=500)
    state: dict[str, Any] = Field(default_factory=dict)
    last_job_id: str | None = Field(default=None, max_length=64)

    @field_validator("last_job_id")
    @classmethod
    def _validate_last_job_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not _JOB_ID_RE.match(cleaned):
            raise ValueError("Invalid last_job_id.")
        return cleaned

    @model_validator(mode="after")
    def _sync_notes_and_description(self) -> ProjectCreateRequest:
        resolved = (
            self.notes
            if (self.notes is not None and self.notes != "")
            else (self.description if self.description is not None else "")
        )
        self.description = resolved
        self.notes = resolved
        return self


class ProjectUpdateRequest(BaseModel):
    """Partial update payload for ``PATCH /api/projects/{project_id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=500)
    state: dict[str, Any] | None = None
    last_job_id: str | None = Field(default=None, max_length=64)

    @field_validator("last_job_id")
    @classmethod
    def _validate_last_job_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not _JOB_ID_RE.match(cleaned):
            raise ValueError("Invalid last_job_id.")
        return cleaned


class ProjectRecord(BaseModel):
    """Persisted Studio trip configuration project."""

    id: str
    project_id: str
    name: str
    description: str = ""
    notes: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    last_job_id: str | None = None
    thumbnail_url: str | None = None
    owner_id: str = "local"
    owner_role: str = "none"
    created_at: str
    updated_at: str


class ProjectsListResponse(BaseModel):
    """Response envelope for ``GET /api/projects``."""

    projects: list[ProjectRecord] = Field(default_factory=list)


def is_valid_project_id(project_id: str) -> bool:
    return bool(_PROJECT_ID_RE.match(project_id))


def _row_to_project(row: sqlite3.Row) -> ProjectRecord:
    pid = str(row["project_id"])
    raw_state = row["state_json"]
    try:
        state = json.loads(raw_state) if raw_state else {}
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    last_job_id = str(row["last_job_id"]) if row["last_job_id"] else None
    thumb_url = (
        f"/api/jobs/{urllib.parse.quote(last_job_id, safe='')}/thumbnail.png"
        if last_job_id and _JOB_ID_RE.match(last_job_id)
        else None
    )
    desc_text = str(row["description"] or "")
    keys = set(row.keys())
    owner_id = str(row["owner_id"]) if "owner_id" in keys and row["owner_id"] else "local"
    owner_role = str(row["owner_role"]) if "owner_role" in keys and row["owner_role"] else "none"
    return ProjectRecord(
        id=pid,
        project_id=pid,
        name=str(row["name"]),
        description=desc_text,
        notes=desc_text,
        state=state,
        last_job_id=last_job_id,
        thumbnail_url=thumb_url,
        owner_id=owner_id,
        owner_role=owner_role,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def list_projects(
    db_path: Path,
    *,
    limit: int = 100,
    owner_id: str | None = None,
) -> list[ProjectRecord]:
    """List saved trip projects ordered most recently updated first."""
    conn = db._connect(db_path)
    try:
        clamped = max(1, min(500, limit))
        if owner_id is not None:
            rows = conn.execute(
                "SELECT * FROM projects WHERE owner_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (owner_id, clamped),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (clamped,),
            ).fetchall()
        return [_row_to_project(r) for r in rows]
    finally:
        conn.close()


def get_project(db_path: Path, project_id: str) -> ProjectRecord | None:
    """Fetch a single saved project by ID."""
    conn = db._connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return _row_to_project(row) if row is not None else None
    finally:
        conn.close()


def create_project(
    db_path: Path,
    req: ProjectCreateRequest,
    *,
    owner_id: str = "local",
    owner_role: str = "none",
    max_per_guest: int | None = None,
    max_guest_projects_global: int | None = None,
    guest_ttl_hours: float | None = None,
) -> ProjectRecord:
    """Persist a new saved trip project and return its record.

    For anonymous ``guest`` projects, enforces a 64 KB serialized state limit and
    bounds storage by automatically pruning expired guest projects and evicting
    the oldest guest projects when either the per-guest or global guest cap is reached.
    """
    project_id = uuid.uuid4().hex[:16]
    ref_now = db._resolve_now(None)
    now = db._dt_to_iso(ref_now)
    state_json = _serialize_project_state(req.state)
    conn = db._connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if owner_role == "guest":
            eff_ttl = (
                guest_ttl_hours
                if guest_ttl_hours is not None
                else _env_pos_float("GRIDFORGE_GUEST_TTL_HOURS", DEFAULT_GUEST_PROJECT_TTL_HOURS)
            )
            if eff_ttl > 0:
                expired_cutoff = db._dt_to_iso(ref_now - timedelta(hours=eff_ttl))
                conn.execute(
                    "DELETE FROM projects WHERE owner_role = 'guest' AND updated_at < ?",
                    (expired_cutoff,),
                )

            per_guest_cap = (
                max(1, int(max_per_guest))
                if max_per_guest is not None
                else _env_pos_int("GRIDFORGE_MAX_PROJECTS_PER_GUEST", DEFAULT_MAX_PROJECTS_PER_GUEST)
            )
            owned_rows = conn.execute(
                """
                SELECT project_id FROM projects
                WHERE owner_role = 'guest' AND owner_id = ?
                ORDER BY updated_at ASC, rowid ASC
                """,
                (owner_id,),
            ).fetchall()
            excess_owned = len(owned_rows) - (per_guest_cap - 1)
            if excess_owned > 0:
                evict_ids = [str(r["project_id"]) for r in owned_rows[:excess_owned]]
                placeholders = ",".join("?" for _ in evict_ids)
                conn.execute(f"DELETE FROM projects WHERE project_id IN ({placeholders})", evict_ids)

            global_cap = (
                max(1, int(max_guest_projects_global))
                if max_guest_projects_global is not None
                else _env_pos_int("GRIDFORGE_MAX_GUEST_PROJECTS", DEFAULT_MAX_GUEST_PROJECTS)
            )
            total_guests = int(
                conn.execute(
                    "SELECT COUNT(*) FROM projects WHERE owner_role = 'guest'"
                ).fetchone()[0]
            )
            excess_global = total_guests - (global_cap - 1)
            if excess_global > 0:
                evict_rows = conn.execute(
                    """
                    SELECT project_id FROM projects
                    WHERE owner_role = 'guest'
                    ORDER BY updated_at ASC, rowid ASC
                    LIMIT ?
                    """,
                    (excess_global,),
                ).fetchall()
                evict_ids = [str(r["project_id"]) for r in evict_rows]
                if evict_ids:
                    placeholders = ",".join("?" for _ in evict_ids)
                    conn.execute(
                        f"DELETE FROM projects WHERE project_id IN ({placeholders})",
                        evict_ids,
                    )

        conn.execute(
            """
            INSERT INTO projects (
                project_id, name, description, state_json, last_job_id,
                owner_id, owner_role, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                req.name.strip(),
                (req.description or "").strip(),
                state_json,
                req.last_job_id,
                owner_id,
                owner_role,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_project(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def update_project(
    db_path: Path,
    project_id: str,
    req: ProjectUpdateRequest,
) -> ProjectRecord | None:
    """Partially update an existing saved project. Returns ``None`` if not found."""
    now = db._utc_now_iso()
    fields = req.model_fields_set
    validated_state_json = (
        _serialize_project_state(req.state)
        if ("state" in fields and req.state is not None)
        else None
    )
    conn = db._connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if existing is None:
            conn.execute("COMMIT")
            return None

        new_name = req.name.strip() if ("name" in fields and req.name is not None) else str(existing["name"])
        if "notes" in fields:
            new_desc = (req.notes or "").strip()
        elif "description" in fields:
            new_desc = (req.description or "").strip()
        else:
            new_desc = str(existing["description"] or "")
        new_state_json = (
            validated_state_json
            if validated_state_json is not None
            else str(existing["state_json"])
        )
        new_last_job = (
            req.last_job_id if "last_job_id" in fields else existing["last_job_id"]
        )

        conn.execute(
            """
            UPDATE projects
            SET name = ?, description = ?, state_json = ?, last_job_id = ?, updated_at = ?
            WHERE project_id = ?
            """,
            (new_name, new_desc, new_state_json, new_last_job, now, project_id),
        )
        updated = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        conn.execute("COMMIT")
        return _row_to_project(updated) if updated is not None else None
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def delete_project(db_path: Path, project_id: str) -> bool:
    """Delete a saved project by ID."""
    conn = db._connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
