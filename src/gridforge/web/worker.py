"""Single-concurrency background render & export worker for GridForge Phase 4.

Runs strictly 1 job at a time with ephemeral spawned PyQGIS subprocesses so peak
memory stays safely below the 3 GB worker container limit on 16 GB hosts.
"""

from __future__ import annotations

import argparse
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

from pyproj import Transformer
from pypdf import PdfWriter

from gridforge.config import SheetSpec
from gridforge.export.kmz import export_garmin_kmz
from gridforge.export.mbtiles import export_mbtiles
from gridforge.export.raster import render_collarless_geotiff
from gridforge.geo.crs import compute_sheet_layout, parse_center
from gridforge.packs.builder import discover_packs, load_pack_manifest
from gridforge.pipeline import runner
from gridforge.render.pyqgis_worker import PyQGISRenderer
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


def _merge_pdfs(sheet_pdfs: list[Path], output_pdf: Path) -> Path:
    """Losslessly merge per-sheet vector PDFs into a single multi-page Atlas PDF."""
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        for pdf_path in sheet_pdfs:
            writer.append(str(pdf_path))
        with output_pdf.open("wb") as fh:
            writer.write(fh)
    finally:
        writer.close()
    return output_pdf


def _iter_atlas_sheet_specs(
    anchor_spec: SheetSpec,
    grid_cols: int,
    grid_rows: int,
    base_title: str,
) -> list[tuple[str, SheetSpec]]:
    """Generate ordered (sheet_id, SheetSpec) pairs for an edge-matched UTM multi-sheet atlas."""
    anchor_layout = compute_sheet_layout(anchor_spec)
    ground_w = anchor_layout.utm_bbox.width
    ground_h = anchor_layout.utm_bbox.height
    to_wgs84 = Transformer.from_crs(
        f"EPSG:{anchor_layout.utm_epsg}", "EPSG:4326", always_xy=True
    )
    total = grid_cols * grid_rows
    sheets: list[tuple[str, SheetSpec]] = []
    idx = 0
    for r in range(grid_rows):
        row_letter = chr(ord("A") + r)
        for c in range(grid_cols):
            idx += 1
            sheet_id = f"{row_letter}{c + 1}"
            sheet_cx = anchor_layout.center_easting + (c - (grid_cols - 1) / 2.0) * ground_w
            sheet_cy = anchor_layout.center_northing + ((grid_rows - 1) / 2.0 - r) * ground_h
            sheet_lon, sheet_lat = to_wgs84.transform(sheet_cx, sheet_cy)
            sheet_spec = anchor_spec.model_copy(
                update={
                    "center_lat": float(sheet_lat),
                    "center_lon": float(sheet_lon),
                    "center_mgrs": None,
                    "title": f"{base_title} — Sheet {sheet_id} ({idx}/{total})",
                }
            )
            sheets.append((sheet_id, sheet_spec))
    return sheets


def _build_prep_kwargs(
    req: CreateJobRequest,
    *,
    claimed: db.ClaimedJob,
    packs_dir: Path,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    overlay_sources = (
        [UserOverlaySource(claimed.overlay_path)]
        if claimed.overlay_path is not None and claimed.overlay_path.is_file()
        else None
    )

    prep_kwargs: dict[str, Any] = {
        "overlay_sources": overlay_sources,
    }

    if progress_cb is not None:
        prep_kwargs["progress_callback"] = progress_cb

    if req.pack_id is not None:
        prep_kwargs["pack_dir"] = _resolve_pack_dir(packs_dir, req.pack_id)
    return prep_kwargs


def process_single_job(
    claimed: db.ClaimedJob,
    *,
    db_path: Path,
    data_dir: Path,
    packs_dir: Path,
    worker_id: str = "worker-1",
) -> JobRecord | None:
    """Execute one claimed job end-to-end (single sheet or multi-sheet UTM atlas)."""
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
        total_sheets = req.grid_cols * req.grid_rows
        outputs = req.outputs
        artifacts: list[JobArtifact] = []

        if total_sheets == 1:
            _report_stage("fetching_data", 15)
            prep_kwargs = _build_prep_kwargs(
                req, claimed=claimed, packs_dir=packs_dir, progress_cb=_report_stage
            )
            workspace = runner.prepare_job_workspace(spec, tmp_work_dir, **prep_kwargs)

            if _is_cancelled(db_path, job_id):
                return db.get_job(db_path, job_id)

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
            anchor_workspace = workspace
        else:
            # Multi-sheet UTM Atlas: render sheets A1..C3 sequentially (1 at a time -> < 1.5 GB RSS)
            base_title = req.title or claimed.record.center_mgrs or "GRIDFORGE ATLAS"
            atlas_sheets = _iter_atlas_sheet_specs(
                spec, req.grid_cols, req.grid_rows, base_title
            )
            sheet_pdfs_dir = tmp_work_dir / "atlas_pdfs"
            sheet_pdfs_dir.mkdir(parents=True, exist_ok=True)
            rendered_sheet_pdfs: list[Path] = []
            pdf_budget_pct = 72 if (outputs.mbtiles or outputs.kmz) else 90

            if outputs.pdf:
                for idx, (sheet_id, sheet_spec) in enumerate(atlas_sheets, start=1):
                    if _is_cancelled(db_path, job_id):
                        return db.get_job(db_path, job_id)

                    sheet_pct = 10 + int(((idx - 1) / total_sheets) * (pdf_budget_pct - 10))
                    _report_stage(f"rendering_sheet_{sheet_id} ({idx}/{total_sheets})", sheet_pct)

                    sheet_work = tmp_work_dir / f"sheet_{sheet_id}"
                    sheet_work.mkdir(parents=True, exist_ok=True)
                    try:
                        prep_kwargs = _build_prep_kwargs(
                            req, claimed=claimed, packs_dir=packs_dir, progress_cb=None
                        )
                        sheet_ws = runner.prepare_job_workspace(
                            sheet_spec, sheet_work, **prep_kwargs
                        )
                        sheet_pdf = sheet_pdfs_dir / f"sheet_{idx:02d}_{sheet_id}.pdf"
                        PyQGISRenderer().render_pdf(sheet_ws, sheet_pdf)
                        rendered_sheet_pdfs.append(sheet_pdf)
                    finally:
                        shutil.rmtree(sheet_work, ignore_errors=True)

                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)

                _report_stage("merging_atlas_pdf", pdf_budget_pct)
                atlas_name = f"{stem}_atlas_{req.grid_cols}x{req.grid_rows}.pdf"
                atlas_pdf_path = artifacts_dir / atlas_name
                _merge_pdfs(rendered_sheet_pdfs, atlas_pdf_path)
                artifacts.append(
                    JobArtifact(
                        kind="pdf",
                        filename=atlas_name,
                        size_bytes=atlas_pdf_path.stat().st_size,
                        download_url=f"/api/jobs/{job_id}/artifacts/{atlas_name}",
                    )
                )

            anchor_workspace = None
            if outputs.mbtiles or outputs.kmz:
                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)
                _report_stage("preparing_center_sheet_export", 74)
                center_work = tmp_work_dir / "center_export_ws"
                center_work.mkdir(parents=True, exist_ok=True)
                prep_kwargs = _build_prep_kwargs(
                    req, claimed=claimed, packs_dir=packs_dir, progress_cb=None
                )
                anchor_workspace = runner.prepare_job_workspace(
                    spec, center_work, **prep_kwargs
                )

        if outputs.mbtiles or outputs.kmz:
            assert anchor_workspace is not None
            eff_dpi = outputs.export_dpi or spec.dpi
            eff_grid = outputs.include_grid_on_digital
            shared_tif: Path | None = None

            if outputs.mbtiles and outputs.kmz:
                _report_stage("rendering_digital_raster", 76)
                shared_tif = render_collarless_geotiff(
                    anchor_workspace,
                    tmp_work_dir / "collarless_export.tif",
                    dpi=eff_dpi,
                    include_grid=eff_grid,
                )

            if outputs.mbtiles:
                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)
                _report_stage("exporting_mbtiles", 82)
                mbt_name = f"{stem}.mbtiles"
                mbt_path = artifacts_dir / mbt_name
                export_mbtiles(
                    anchor_workspace,
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
                _report_stage("exporting_kmz", 92)
                kmz_name = f"{stem}.kmz"
                kmz_path = artifacts_dir / kmz_name
                export_garmin_kmz(
                    anchor_workspace,
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
