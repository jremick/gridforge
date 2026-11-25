"""Single-concurrency background render & export worker for GridForge Phase 4.

Runs strictly 1 job at a time with ephemeral spawned PyQGIS subprocesses so peak
memory stays safely below the 3 GB worker container limit on 16 GB hosts.
"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
import re
import resource
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from gridforge.config import SheetSpec
from gridforge.export.kmz import export_garmin_kmz
from gridforge.export.mbtiles import export_mbtiles
from gridforge.export.raster import render_collarless_geotiff
from gridforge.geo.crs import parse_center
from gridforge.packs.builder import discover_packs, load_pack_manifest
from gridforge.pipeline import runner
from gridforge.render.pyqgis_worker import PyQGISRenderer
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource
from gridforge.sources.overlay_user import UserOverlaySource
from gridforge.web import db
from gridforge.web.schemas import CreateJobRequest, JobArtifact, JobRecord, JobStatus

logger = logging.getLogger(__name__)


def _peak_rss_mb() -> float:
    """Measure peak process/child RSS in megabytes (handles Linux KiB vs macOS bytes)."""
    self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak = max(self_rss, child_rss)
    if sys.platform == "darwin":
        return peak / (1024.0 * 1024.0)
    return peak / 1024.0


def _artifact_stem(req: CreateJobRequest, center_mgrs: str | None) -> str:
    raw_base = req.title or center_mgrs or "gridforge_map"
    base = re.sub(r"[^a-zA-Z0-9]+", "_", raw_base.lower()).strip("_")[:48] or "gridforge_map"
    scale_k = f"{req.scale // 1000}k"
    return f"{base}_{scale_k}_{req.paper.value}_{req.color_mode.value}"


def _resolve_pack_dir(packs_dir: Path, pack_id: str) -> Path:
    """Locate the directory containing ``pack.json`` for ``pack_id`` safely under ``packs_dir``."""
    direct = (packs_dir / pack_id).resolve()
    if direct.is_relative_to(packs_dir.resolve()) and (direct / "pack.json").is_file():
        return direct
    for child in sorted(packs_dir.iterdir()) if packs_dir.is_dir() else []:
        if child.is_dir() and (child / "pack.json").is_file():
            try:
                manifest = load_pack_manifest(child)
                if manifest.pack_id == pack_id:
                    return child.resolve()
            except Exception:
                continue
    raise FileNotFoundError(f"Offline pack '{pack_id}' not found in '{packs_dir}'.")


class _PackLocalDEMSource(LocalDEMSource):
    """LocalDEMSource carrying exact vertical_datum and attribution from a PackManifest."""

    def __init__(
        self,
        path: Path | str,
        *,
        vertical_datum: str = "AHD",
        attribution: str | None = None,
    ) -> None:
        super().__init__(path)
        self.vertical_datum = vertical_datum
        self.resolved_vertical_datum = vertical_datum
        if attribution:
            self.attribution = attribution
            self.resolved_attribution = attribution


def _build_spec_from_request(req: CreateJobRequest) -> SheetSpec:
    assert req.center is not None
    lat, lon, canonical_mgrs = parse_center(req.center)
    return SheetSpec(
        center_lat=lat,
        center_lon=lon,
        center_mgrs=canonical_mgrs,
        scale=req.scale,
        paper=req.paper,
        orientation=req.orientation,
        color_mode=req.color_mode,
        units=req.units,
        title=req.title,
        date_iso=req.date_iso,
        geopdf=req.outputs.geopdf,
        dpi=req.dpi,
        carto=req.carto,
        dem_source=req.dem_source,
        us_overlays=req.us_overlays,
        au_overlays=req.au_overlays,
    )


def _is_cancelled(db_path: Path, job_id: str) -> bool:
    rec = db.get_job(db_path, job_id)
    return rec is None or rec.status == JobStatus.CANCELLED


def process_single_job(
    claimed: db.ClaimedJob,
    *,
    db_path: Path,
    data_dir: Path,
    packs_dir: Path,
    worker_id: str = "worker-1",
) -> JobRecord | None:
    """Execute one claimed job end-to-end and record artifacts or failure in SQLite."""
    t0 = time.perf_counter()
    job_id = claimed.record.job_id
    artifacts_dir = data_dir / job_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    tmp_work_dir = Path(tempfile.mkdtemp(prefix=f"gf_worker_{job_id}_")).resolve()

    def _report_stage(stage: str, pct: int) -> None:
        db.touch_heartbeat(db_path, worker_id=worker_id, pid=os.getpid(), active_job_id=job_id)
        db.update_job_progress(db_path, job_id, stage=stage, progress_pct=pct)

    try:
        req = CreateJobRequest.model_validate_json(claimed.request_json)
        spec = _build_spec_from_request(req)
        stem = _artifact_stem(req, claimed.record.center_mgrs)

        _report_stage("fetching_data", 15)

        overlay_sources = (
            [UserOverlaySource(claimed.overlay_path)]
            if claimed.overlay_path is not None and claimed.overlay_path.is_file()
            else None
        )

        # Build kwargs for runner.prepare_job_workspace, cleanly supporting pack_dir & progress_callback
        prep_sig = inspect.signature(runner.prepare_job_workspace)
        prep_kwargs: dict[str, Any] = {
            "overlay_sources": overlay_sources,
        }

        if "progress_callback" in prep_sig.parameters:
            prep_kwargs["progress_callback"] = _report_stage

        if req.pack_id is not None:
            pack_dir = _resolve_pack_dir(packs_dir, req.pack_id)
            if "pack_dir" in prep_sig.parameters:
                prep_kwargs["pack_dir"] = pack_dir
            else:
                manifest = load_pack_manifest(pack_dir)
                if manifest.dem_path:
                    prep_kwargs["elevation_source"] = _PackLocalDEMSource(
                        pack_dir / manifest.dem_path,
                        vertical_datum=manifest.vertical_datum,
                        attribution=manifest.dem_attribution,
                    )
                if manifest.osm_pbf_path:
                    prep_kwargs["vector_source"] = LocalPBFSource(
                        pack_dir / manifest.osm_pbf_path
                    )
                if manifest.overlays:
                    prep_kwargs["regional_overlay_overrides"] = {
                        k: (pack_dir / rel) for k, rel in manifest.overlays.items()
                    }

        workspace = runner.prepare_job_workspace(spec, tmp_work_dir, **prep_kwargs)

        if _is_cancelled(db_path, job_id):
            return db.get_job(db_path, job_id)

        artifacts: list[JobArtifact] = []
        outputs = req.outputs

        # 1. Print PDF
        if outputs.pdf:
            _report_stage("rendering_pdf", 50)
            pdf_name = f"{stem}.pdf"
            pdf_path = artifacts_dir / pdf_name
            PyQGISRenderer().render_pdf(workspace, pdf_path)
            artifacts.append(
                JobArtifact(
                    kind="pdf",
                    filename=pdf_name,
                    size_bytes=pdf_path.stat().st_size,
                    download_url=f"/api/jobs/{job_id}/artifacts/{pdf_name}",
                )
            )

        if _is_cancelled(db_path, job_id):
            return db.get_job(db_path, job_id)

        # 2. Digital exports (share one collarless GeoTIFF when both MBTiles & KMZ requested)
        eff_dpi = outputs.export_dpi or spec.dpi
        eff_grid = outputs.include_grid_on_digital
        shared_tif: Path | None = None

        if outputs.mbtiles and outputs.kmz:
            _report_stage("rendering_digital_raster", 68)
            shared_tif = render_collarless_geotiff(
                workspace,
                tmp_work_dir / "collarless_export.tif",
                dpi=eff_dpi,
                include_grid=eff_grid,
            )

        if outputs.mbtiles:
            if _is_cancelled(db_path, job_id):
                return db.get_job(db_path, job_id)
            _report_stage("exporting_mbtiles", 78)
            mbt_name = f"{stem}.mbtiles"
            mbt_path = artifacts_dir / mbt_name
            export_mbtiles(
                workspace,
                mbt_path,
                tile_format=outputs.mbtiles_format,
                dpi=eff_dpi,
                include_grid=eff_grid,
                collarless_tif=shared_tif,
            )
            artifacts.append(
                JobArtifact(
                    kind="mbtiles",
                    filename=mbt_name,
                    size_bytes=mbt_path.stat().st_size,
                    download_url=f"/api/jobs/{job_id}/artifacts/{mbt_name}",
                )
            )

        if outputs.kmz:
            if _is_cancelled(db_path, job_id):
                return db.get_job(db_path, job_id)
            _report_stage("exporting_kmz", 90)
            kmz_name = f"{stem}.kmz"
            kmz_path = artifacts_dir / kmz_name
            export_garmin_kmz(
                workspace,
                kmz_path,
                profile=outputs.garmin_profile,
                max_tiles=outputs.max_tiles,
                dpi=eff_dpi,
                include_grid=eff_grid,
                collarless_tif=shared_tif,
            )
            artifacts.append(
                JobArtifact(
                    kind="kmz",
                    filename=kmz_name,
                    size_bytes=kmz_path.stat().st_size,
                    download_url=f"/api/jobs/{job_id}/artifacts/{kmz_name}",
                )
            )

        if _is_cancelled(db_path, job_id):
            return db.get_job(db_path, job_id)

        elapsed = time.perf_counter() - t0
        rss_mb = _peak_rss_mb()
        return db.complete_job(
            db_path,
            job_id,
            artifacts=artifacts,
            elapsed_seconds=elapsed,
            peak_rss_mb=rss_mb,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        rss_mb = _peak_rss_mb()
        logger.exception("Job %s failed after %.2fs: %s", job_id, elapsed, exc)
        return db.fail_job(
            db_path,
            job_id,
            error=str(exc),
            elapsed_seconds=elapsed,
            peak_rss_mb=rss_mb,
        )
    finally:
        shutil.rmtree(tmp_work_dir, ignore_errors=True)
        db.touch_heartbeat(db_path, worker_id=worker_id, pid=os.getpid(), active_job_id=None)


def run_worker_loop(
    *,
    data_dir: Path,
    packs_dir: Path,
    worker_id: str = "worker-1",
    poll_seconds: float = 1.0,
    heartbeat_seconds: float = 5.0,
    prune_interval_seconds: float = 900.0,
    max_job_age_hours: float = 24.0,
    once: bool = False,
) -> None:
    """Run the single-concurrency SQLite worker loop with graceful shutdown."""
    db_path = data_dir / "gridforge_jobs.sqlite3"
    db.init_db(db_path)

    orphaned = db.reset_orphaned_running_jobs(db_path)
    if orphaned:
        logger.warning("Reset %d orphaned running job(s) on startup", orphaned)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down GridForge worker cleanly...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    last_heartbeat = 0.0
    last_prune = 0.0
    packs_count = len(discover_packs(packs_dir))
    logger.info(
        "GridForge worker '%s' started (pid=%d, data_dir=%s, packs_dir=%s, packs=%d, concurrency=1)",
        worker_id,
        os.getpid(),
        data_dir,
        packs_dir,
        packs_count,
    )

    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_seconds:
            db.touch_heartbeat(db_path, worker_id=worker_id, pid=os.getpid(), active_job_id=None)
            last_heartbeat = now

        if now - last_prune >= prune_interval_seconds:
            pruned = db.prune_expired_jobs(
                db_path, data_dir=data_dir, max_age_hours=max_job_age_hours
            )
            if pruned:
                logger.info("Pruned %d expired job(s) older than %.1fh", pruned, max_job_age_hours)
            last_prune = now

        claimed = db.claim_next_job(db_path)
        if claimed is not None:
            logger.info("Claimed job %s (scale=1:%d, paper=%s)", claimed.record.job_id, claimed.record.scale, claimed.record.paper.value)
            process_single_job(
                claimed,
                db_path=db_path,
                data_dir=data_dir,
                packs_dir=packs_dir,
                worker_id=worker_id,
            )
            last_heartbeat = time.monotonic()
            if once:
                break
            continue

        if once:
            break
        stop_event.wait(timeout=poll_seconds)

    logger.info("GridForge worker '%s' exited cleanly.", worker_id)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("GRIDFORGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="GridForge Phase 4 single-concurrency map worker")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("GRIDFORGE_DATA_DIR", "/data/jobs")),
    )
    parser.add_argument(
        "--packs-dir",
        type=Path,
        default=Path(os.environ.get("GRIDFORGE_PACKS_DIR", "/data/packs")),
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=os.environ.get("GRIDFORGE_WORKER_ID", "worker-1"),
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=float(os.environ.get("GRIDFORGE_JOB_TTL_HOURS", "24")),
    )
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    args = parser.parse_args(argv)

    run_worker_loop(
        data_dir=args.data_dir.resolve(),
        packs_dir=args.packs_dir.resolve(),
        worker_id=args.worker_id,
        max_job_age_hours=args.max_age_hours,
        once=args.once,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
