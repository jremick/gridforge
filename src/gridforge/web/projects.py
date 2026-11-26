"""SQLite-backed Saved Trip Projects CRUD store for GridForge Studio."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from gridforge.web import db

_PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")


class ProjectCreateRequest(BaseModel):
    """Request payload for ``POST /api/projects``."""

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=500)
    state: dict[str, Any] = Field(default_factory=dict)
    last_job_id: str | None = Field(default=None, max_length=64)

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
    thumb_url = f"/api/jobs/{last_job_id}/thumbnail.png" if last_job_id else None
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
) -> ProjectRecord:
    """Persist a new saved trip project and return its record."""
    project_id = uuid.uuid4().hex[:16]
    now = db._utc_now_iso()
    state_json = json.dumps(req.state, separators=(",", ":"))
    conn = db._connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
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
            json.dumps(req.state, separators=(",", ":"))
            if ("state" in fields and req.state is not None)
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
