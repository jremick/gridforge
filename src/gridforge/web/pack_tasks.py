"""Single-concurrency background offline regional pack builder & manager for Studio API."""

from __future__ import annotations

import inspect
import logging
import queue
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from gridforge.config import BBox, PackManifest
from gridforge.packs import catalog as packs_catalog
from gridforge.packs.builder import build_offline_pack, load_pack_manifest
from gridforge.packs.catalog import RegionPreset, resolve_region

logger = logging.getLogger(__name__)

_PACK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return (slug[:60].strip("-")) or "pack"


class PackRegionSummary(BaseModel):
    """Curated regional pack preset exposed by ``GET /api/packs/regions``."""

    id: str
    region_id: str
    name: str
    group: Literal["au_tactical", "au_state", "nz", "na", "custom"]
    kind: Literal["state", "tactical", "custom"]
    category: Literal["state", "tactical", "custom"]
    bbox_wgs84: BBox
    utm_zones: list[str]
    description: str = ""
    estimated_size_hint: str = ""


class PackRegionsResponse(BaseModel):
    """Response envelope for ``GET /api/packs/regions``."""

    regions: list[PackRegionSummary] = Field(default_factory=list)


class PackBuildRequest(BaseModel):
    """Request payload for ``POST /api/packs/build``."""

    region_or_bbox: str = Field(..., min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=120)
    include_osm: bool = True
    include_dem: bool = True
    include_overlays: bool = True
    include_basemap: bool = True
    dem_tier: Literal["auto", "srtm-1s", "glo30", "au-elvis", "nz-linz", "usgs3dep", "ca-hrdem"] = "srtm-1s"


class PackTaskRecord(BaseModel):
    """Live status & result record for a background offline pack build task."""

    task_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    region: str
    region_name: str
    output_dir: str
    pack_id: str | None = None
    stage: str = "queued"
    message: str = "Queued for offline pack build"
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    error: str | None = None
    manifest: PackManifest | None = None


class PackTasksListResponse(BaseModel):
    """Response envelope for ``GET /api/packs/tasks``."""

    tasks: list[PackTaskRecord] = Field(default_factory=list)


def _infer_region_group(preset: Any) -> Literal["au_tactical", "au_state", "nz", "na", "custom"]:
    explicit = getattr(preset, "group", None)
    if explicit in ("au_tactical", "au_state", "nz", "na", "custom"):
        return explicit
    cat = getattr(preset, "category", "tactical")
    if cat == "state":
        return "au_state"
    if cat == "tactical":
        return "au_tactical"
    return "custom"


def _load_all_regions() -> list[Any]:
    try:
        from gridforge.packs import regions as packs_regions  # type: ignore[import-not-found]

        if hasattr(packs_regions, "list_regions"):
            return list(packs_regions.list_regions())
    except Exception:
        pass
    return list(packs_catalog.list_regions())


def _resolve_any_region(region_or_bbox: str) -> Any:
    try:
        from gridforge.packs import regions as packs_regions  # type: ignore[import-not-found]

        if hasattr(packs_regions, "resolve_region"):
            return packs_regions.resolve_region(region_or_bbox)
    except Exception:
        pass
    return resolve_region(region_or_bbox)


def get_pack_regions_response() -> PackRegionsResponse:
    """Build the curated grouped regional pack catalog payload (AU, NZ, NA)."""
    items = [
        PackRegionSummary(
            id=r.region_id,
            region_id=r.region_id,
            name=r.name,
            group=_infer_region_group(r),
            kind=r.category if r.category in ("state", "tactical", "custom") else "tactical",
            category=r.category if r.category in ("state", "tactical", "custom") else "tactical",
            bbox_wgs84=r.bbox_wgs84,
            utm_zones=list(r.utm_zones),
            description=getattr(r, "description", "") or "",
            estimated_size_hint=getattr(r, "estimated_size_hint", "") or "",
        )
        for r in _load_all_regions()
    ]
    return PackRegionsResponse(regions=items)


class PackTaskManager:
    """Strictly single-concurrency FIFO offline pack builder & safe pack deleter."""

    def __init__(
        self,
        packs_dir: Path,
        *,
        pack_builder: Callable[..., PackManifest] | None = None,
    ) -> None:
        self._packs_dir = Path(packs_dir).resolve()
        self._builder: Callable[..., PackManifest] = pack_builder or build_offline_pack
        self._lock = threading.Lock()
        self._tasks: dict[str, PackTaskRecord] = {}
        self._task_order: list[str] = []
        self._active_task_id: str | None = None
        self._queue: queue.Queue[tuple[str, PackBuildRequest, Path]] = queue.Queue()
        self._worker_started = False

    def _ensure_worker_started(self) -> None:
        if self._worker_started:
            return
        self._worker_started = True
        thread = threading.Thread(
            target=self._worker_loop,
            name="gridforge-pack-builder",
            daemon=True,
        )
        thread.start()

    def _resolve_target_dir(self, preset: RegionPreset, custom_name: str | None) -> tuple[str, Path]:
        if preset.category == "custom" and custom_name and custom_name.strip():
            slug = _slugify(custom_name)
        else:
            slug = _slugify(preset.region_id)

        if not _PACK_ID_RE.match(slug):
            raise ValueError(f"Derived pack directory name '{slug}' is invalid.")

        target = (self._packs_dir / slug).resolve()
        if not target.is_relative_to(self._packs_dir) or target == self._packs_dir:
            raise ValueError("Pack output directory escapes configured packs directory.")
        return (slug, target)

    def enqueue_build(self, req: PackBuildRequest) -> PackTaskRecord:
        """Validate region/bbox and enqueue a single-concurrency pack build task."""
        preset = _resolve_any_region(req.region_or_bbox)
        slug, out_dir = self._resolve_target_dir(preset, req.name)

        task_id = uuid.uuid4().hex[:16]
        now_iso = _utc_now_iso()

        with self._lock:
            idle = self._active_task_id is None and self._queue.empty()
            initial_status: Literal["queued", "running"] = "running" if idle else "queued"
            record = PackTaskRecord(
                task_id=task_id,
                status=initial_status,
                region=preset.region_id,
                region_name=(req.name.strip() if req.name and req.name.strip() else preset.name),
                output_dir=str(out_dir),
                pack_id=slug,
                stage="building_pack" if idle else "queued",
                message=(
                    f"Building offline pack '{preset.name}'..."
                    if idle
                    else f"Queued offline pack '{preset.name}' (waiting for active build)"
                ),
                created_at=now_iso,
                started_at=now_iso if idle else None,
            )
            self._tasks[task_id] = record
            self._task_order.append(task_id)
            if idle:
                self._active_task_id = task_id
            self._ensure_worker_started()

        self._queue.put((task_id, req, out_dir))
        return record.model_copy(deep=True)

    def _worker_loop(self) -> None:
        while True:
            task_id, req, out_dir = self._queue.get()
            t0 = time.perf_counter()
            try:
                with self._lock:
                    self._active_task_id = task_id
                    rec = self._tasks[task_id]
                    self._tasks[task_id] = rec.model_copy(
                        update={
                            "status": "running",
                            "stage": "building_pack",
                            "message": f"Building offline pack '{rec.region_name}'...",
                            "started_at": rec.started_at or _utc_now_iso(),
                        }
                    )

                self._packs_dir.mkdir(parents=True, exist_ok=True)
                build_kwargs: dict[str, Any] = {
                    "name": req.name,
                    "include_osm": req.include_osm,
                    "include_dem": req.include_dem,
                    "include_overlays": req.include_overlays,
                    "dem_tier": req.dem_tier,
                }
                sig = inspect.signature(self._builder)
                if "include_basemap" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    build_kwargs["include_basemap"] = req.include_basemap
                manifest = self._builder(
                    req.region_or_bbox,
                    out_dir,
                    **build_kwargs,
                )
                elapsed = round(time.perf_counter() - t0, 3)
                with self._lock:
                    rec = self._tasks[task_id]
                    self._tasks[task_id] = rec.model_copy(
                        update={
                            "status": "succeeded",
                            "stage": "completed",
                            "message": f"Built pack '{manifest.name}' ({manifest.size_bytes / (1024 * 1024):.1f} MB)",
                            "pack_id": manifest.pack_id,
                            "finished_at": _utc_now_iso(),
                            "elapsed_seconds": elapsed,
                            "manifest": manifest,
                            "error": None,
                        }
                    )
            except Exception as exc:
                elapsed = round(time.perf_counter() - t0, 3)
                logger.exception("Pack build task %s failed after %.2fs: %s", task_id, elapsed, exc)
                with self._lock:
                    rec = self._tasks[task_id]
                    self._tasks[task_id] = rec.model_copy(
                        update={
                            "status": "failed",
                            "stage": "failed",
                            "message": f"Pack build failed: {exc}",
                            "finished_at": _utc_now_iso(),
                            "elapsed_seconds": elapsed,
                            "error": str(exc),
                        }
                    )
            finally:
                with self._lock:
                    if self._active_task_id == task_id:
                        self._active_task_id = None
                self._queue.task_done()

    def list_tasks(self) -> list[PackTaskRecord]:
        with self._lock:
            return [
                self._tasks[tid].model_copy(deep=True)
                for tid in reversed(self._task_order)
            ]

    def get_task(self, task_id: str) -> PackTaskRecord | None:
        if not _TASK_ID_RE.match(task_id):
            return None
        with self._lock:
            rec = self._tasks.get(task_id)
            return rec.model_copy(deep=True) if rec is not None else None

    def is_pack_busy(self, pack_id: str) -> bool:
        with self._lock:
            for rec in self._tasks.values():
                if rec.status in ("queued", "running") and rec.pack_id == pack_id:
                    return True
            return False

    def find_pack_dir(self, pack_id: str) -> Path | None:
        """Locate a pack directory by manifest ``pack_id`` strictly inside ``packs_dir``."""
        if (
            not pack_id
            or not _PACK_ID_RE.match(pack_id)
            or "/" in pack_id
            or "\\" in pack_id
            or ".." in pack_id
            or "\x00" in pack_id
        ):
            raise ValueError(f"Invalid pack_id '{pack_id}'.")

        if not self._packs_dir.is_dir():
            return None

        for child in sorted(self._packs_dir.iterdir()):
            if not child.is_dir() or not (child / "pack.json").is_file():
                continue
            resolved_child = child.resolve()
            if not resolved_child.is_relative_to(self._packs_dir) or resolved_child == self._packs_dir:
                continue
            try:
                manifest = load_pack_manifest(resolved_child)
                if manifest.pack_id == pack_id:
                    return resolved_child
            except Exception:
                continue

        return None

    def delete_pack(self, pack_id: str) -> bool:
        """Safely delete a built offline pack directory strictly inside ``packs_dir``."""
        target = self.find_pack_dir(pack_id)
        if target is None:
            return False
        if self.is_pack_busy(pack_id):
            raise RuntimeError(f"Offline pack '{pack_id}' is currently being built and cannot be deleted.")
        # Final containment guard against symlink tricks
        resolved = target.resolve()
        if not resolved.is_relative_to(self._packs_dir) or resolved == self._packs_dir:
            raise ValueError(f"Refusing to delete path outside packs directory for '{pack_id}'.")
        shutil.rmtree(resolved)
        return True
