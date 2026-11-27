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

from pyproj import Transformer

from gridforge.config import ColorMode, DeclinationInfo, SheetLayout, SheetSpec
from gridforge.export.bundle import export_field_bundle_zip
from gridforge.export.gpkg_tiles import export_gpkg_tiles
from gridforge.export.kmz import export_garmin_kmz
from gridforge.export.mbtiles import export_mbtiles
from gridforge.export.osmand import export_osmand_sqlitedb
from gridforge.export.raster import render_collarless_geotiff
from gridforge.geo.crs import compute_atlas_layouts, compute_sheet_layout, parse_center
from gridforge.geo.strip_atlas import (
    StripAtlasPlan,
    _format_mgrs_10m,
    parse_route_file,
    plan_strip_atlas,
    sample_route_elevation,
)
from gridforge.geo.supplements import (
    AtlasIndexSheet,
    merge_pdf_pages,
    render_atlas_index_pdf,
    render_calibration_pdf,
    render_elevation_profile_pdf,
    render_rf_link_card_pdf,
    render_route_card_pdf,
)
from gridforge.geo.waypoints import compute_route_metrics
from gridforge.packs.builder import discover_packs, resolve_pack_directory
from gridforge.pipeline import runner, terrain as terrain_mod
from gridforge.render.pyqgis_worker import PyQGISRenderer
from gridforge.sources.overlay_user import UserOverlaySource
from gridforge.web import cache_prune, db
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
    scale_k = f"{req.scale // 1000}k" if req.scale % 1000 == 0 else f"{req.scale / 1000:g}k"
    return f"{base}_{scale_k}_{req.paper.value}_{req.color_mode.value}"


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
        nz_overlays=req.nz_overlays,
        ca_overlays=req.ca_overlays,
        waypoints=req.waypoints,
        viewsheds=req.viewsheds,
        rf_links=req.rf_links,
        tactical_graphics=req.tactical_graphics,
        include_calibration_page=req.include_calibration_page,
        include_route_card=req.include_route_card,
        include_elevation_profile=req.include_elevation_profile,
        include_atlas_index=req.include_atlas_index,
        include_rf_link_card=req.include_rf_link_card,
        show_fold_marks=req.show_fold_marks,
    )


def _is_cancelled(db_path: Path, job_id: str) -> bool:
    rec = db.get_job(db_path, job_id)
    return rec is None or rec.status == JobStatus.CANCELLED


def _merge_pdfs(sheet_pdfs: list[Path], output_pdf: Path) -> Path:
    """Losslessly merge per-sheet vector PDFs into a single multi-page Atlas PDF."""
    return merge_pdf_pages(sheet_pdfs, output_pdf)


def _extract_route_lonlat(
    req: CreateJobRequest,
    overlay_path: Path | None,
) -> list[tuple[float, float]] | None:
    if overlay_path is not None and overlay_path.is_file():
        try:
            return parse_route_file(overlay_path)
        except Exception as exc:
            logger.warning("Could not parse overlay as route (%s)", exc)
    if len(req.waypoints) >= 2:
        return [(w.lon, w.lat) for w in req.waypoints]
    if len(req.strip_sheets) >= 2:
        return [(s.center_lon, s.center_lat) for s in req.strip_sheets]
    return None


def _assemble_supplemental_pages(
    *,
    req: CreateJobRequest,
    spec: SheetSpec,
    map_pdfs: list[Path],
    anchor_layout: SheetLayout,
    anchor_declination: DeclinationInfo,
    dem_paths: list[Path],
    overlay_path: Path | None,
    strip_plan: StripAtlasPlan | None,
    tmp_dir: Path,
    final_pdf: Path,
    atlas_index_sheets: list[AtlasIndexSheet] | None = None,
    atlas_overlap_pct: float = 0.0,
    atlas_title: str | None = None,
) -> Path:
    """Build requested Atlas Index Cover, Elevation Profile, Route Card, and 100 mm Calibration pages and merge."""
    bw = spec.color_mode == ColorMode.BW
    front_pages: list[Path] = []
    back_pages: list[Path] = []
    supp_dir = tmp_dir / "supplements"
    supp_dir.mkdir(parents=True, exist_ok=True)

    route_lonlat = _extract_route_lonlat(req, overlay_path)
    will_render_elev = bool(
        req.include_elevation_profile
        and dem_paths
        and route_lonlat is not None
        and len(route_lonlat) >= 2
    )
    will_render_index = bool(req.include_atlas_index and atlas_index_sheets)
    from gridforge.geo.supplements import (
        _build_atlas_index_payload,
        _build_calibration_payload,
        _build_elevation_profile_payload,
        _build_rf_link_card_payload,
        _build_route_card_payload,
        _spawn_supplement_batch,
    )

    first_map_page = 1 + (1 if will_render_index else 0) + (1 if will_render_elev else 0)
    batch_tasks: list[tuple[str, dict[str, Any], Path]] = []

    # 0. Optional Atlas Overview & Sheet Index Cover Page (Page 1)
    if will_render_index and atlas_index_sheets:
        indexed_sheets = [
            sh.model_copy(update={"page_number": first_map_page + idx})
            for idx, sh in enumerate(atlas_index_sheets)
        ]
        route_utm: list[tuple[float, float]] | None = None
        if route_lonlat is not None and len(route_lonlat) >= 2:
            to_utm = Transformer.from_crs(
                "EPSG:4326", f"EPSG:{anchor_layout.utm_epsg}", always_xy=True
            )
            xs, ys = to_utm.transform(
                [p[0] for p in route_lonlat],
                [p[1] for p in route_lonlat],
            )
            route_utm = [(float(x), float(y)) for x, y in zip(xs, ys)]

        idx_pdf = supp_dir / "atlas_index.pdf"
        batch_tasks.append(
            (
                "atlas-index",
                _build_atlas_index_payload(
                    indexed_sheets,
                    page_width_mm=anchor_layout.page_width_mm,
                    page_height_mm=anchor_layout.page_height_mm,
                    scale=spec.scale,
                    utm_epsg=anchor_layout.utm_epsg,
                    title=atlas_title or spec.title or f"SHEET ATLAS ({anchor_layout.gzd})",
                    overlap_pct=atlas_overlap_pct,
                    route_utm=route_utm,
                    bw=bw,
                ),
                idx_pdf,
            )
        )
        front_pages.append(idx_pdf)

    # 1. Optional Route Overview & Elevation Profile page
    if will_render_elev and route_lonlat is not None:
        profile = sample_route_elevation(route_lonlat, dem_paths, anchor_layout.utm_epsg)
        elev_pdf = supp_dir / "elevation_profile.pdf"
        batch_tasks.append(
            (
                "elevation-profile",
                _build_elevation_profile_payload(profile, strip_plan, anchor_layout, bw=bw),
                elev_pdf,
            )
        )
        front_pages.append(elev_pdf)

    # 2. Optional Tactical Route Card page
    if req.include_route_card and req.waypoints:
        dem_for_wp = dem_paths[0] if dem_paths else None
        route_metrics = compute_route_metrics(
            req.waypoints,
            units=spec.units,
            date_iso=spec.date_iso,
            dem_path=dem_for_wp,
        )
        card_pdf = supp_dir / "route_card.pdf"
        batch_tasks.append(
            (
                "route-card",
                _build_route_card_payload(route_metrics, anchor_layout, anchor_declination, bw=bw),
                card_pdf,
            )
        )
        back_pages.append(card_pdf)

    # 3. Optional RF / Line-of-Sight Link Budget & Fresnel Card page
    if req.include_rf_link_card and req.rf_links and dem_paths:
        compute_rf_fn = getattr(terrain_mod, "compute_rf_link_profile", None)
        if compute_rf_fn is not None:
            sig = inspect.signature(compute_rf_fn)
            rf_kwargs: dict[str, Any] = {}
            if "date_iso" in sig.parameters and spec.date_iso is not None:
                rf_kwargs["date_iso"] = spec.date_iso
            rf_profiles = [
                compute_rf_fn(dem_paths[0], link, anchor_layout.utm_epsg, **rf_kwargs)
                for link in req.rf_links
            ]
            rf_pdf = supp_dir / "rf_link_card.pdf"
            batch_tasks.append(
                (
                    "rf-link-card",
                    _build_rf_link_card_payload(rf_profiles, anchor_layout, bw=bw),
                    rf_pdf,
                )
            )
            back_pages.append(rf_pdf)

    # 4. Optional 100 mm Scale Calibration & MGRS Romer page
    if req.include_calibration_page:
        cal_pdf = supp_dir / "calibration.pdf"
        batch_tasks.append(
            (
                "calibration",
                _build_calibration_payload(
                    anchor_layout.page_width_mm,
                    anchor_layout.page_height_mm,
                    spec.scale,
                    bw=bw,
                ),
                cal_pdf,
            )
        )
        back_pages.append(cal_pdf)

    if batch_tasks:
        _spawn_supplement_batch(batch_tasks)

    all_pages = [*front_pages, *map_pdfs, *back_pages]
    if len(all_pages) == 1 and all_pages[0].resolve() == final_pdf.resolve():
        return final_pdf
    return merge_pdf_pages(all_pages, final_pdf)


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
        prep_kwargs["pack_dir"] = resolve_pack_directory(packs_dir, req.pack_id)
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
    if db.get_job(db_path, job_id) is None:
        shutil.rmtree(data_dir / job_id, ignore_errors=True)
        return None
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
        is_strip = len(req.strip_sheets) > 0
        total_sheets = len(req.strip_sheets) if is_strip else (req.grid_cols * req.grid_rows)
        outputs = req.outputs
        artifacts: list[JobArtifact] = []
        has_supplements = (
            req.include_calibration_page
            or (req.include_route_card and len(req.waypoints) > 0)
            or req.include_elevation_profile
            or (req.include_atlas_index and (is_strip or total_sheets > 1))
            or (req.include_rf_link_card and len(req.rf_links) > 0)
        )
        wants_digital_raster = bool(
            outputs.mbtiles or outputs.kmz or outputs.gpkg_tiles or outputs.osmand_sqlitedb
        )
        wants_any_export = bool(wants_digital_raster or outputs.field_bundle_zip)

        if not is_strip and total_sheets == 1:
            _report_stage("fetching_data", 15)
            prep_kwargs = _build_prep_kwargs(
                req, claimed=claimed, packs_dir=packs_dir, progress_cb=_report_stage
            )
            workspace = runner.prepare_job_workspace(spec, tmp_work_dir, **prep_kwargs)

            if _is_cancelled(db_path, job_id):
                return db.get_job(db_path, job_id)

            # 1. Print PDF (+ optional supplemental pages)
            if outputs.pdf:
                _report_stage("rendering_pdf", 50)
                pdf_name = f"{stem}.pdf"
                pdf_path = artifacts_dir / pdf_name
                if has_supplements:
                    raw_map_pdf = tmp_work_dir / "map_sheet_01.pdf"
                    PyQGISRenderer().render_pdf(workspace, raw_map_pdf)
                    _report_stage("rendering_supplements", 70)
                    dem_list = [workspace.dem_utm_tif] if workspace.dem_utm_tif.is_file() else []
                    _assemble_supplemental_pages(
                        req=req,
                        spec=spec,
                        map_pdfs=[raw_map_pdf],
                        anchor_layout=workspace.layout,
                        anchor_declination=workspace.declination,
                        dem_paths=dem_list,
                        overlay_path=claimed.overlay_path,
                        strip_plan=None,
                        tmp_dir=tmp_work_dir,
                        final_pdf=pdf_path,
                    )
                else:
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
            # Multi-sheet UTM Atlas OR Route Strip Atlas: render sheets sequentially (1 at a time -> < 1.5 GB RSS)
            base_title = req.title or claimed.record.center_mgrs or (
                "GRIDFORGE STRIP ATLAS" if is_strip else "GRIDFORGE ATLAS"
            )
            strip_plan_obj: StripAtlasPlan | None = None
            atlas_sheets: list[tuple[str, SheetSpec]] = []
            atlas_index_sheets: list[AtlasIndexSheet] = []
            atlas_overlap_pct = 0.0

            if is_strip:
                atlas_overlap_pct = 10.0
                if claimed.overlay_path is not None and claimed.overlay_path.is_file():
                    try:
                        strip_plan_obj = plan_strip_atlas(
                            parse_route_file(claimed.overlay_path),
                            scale=spec.scale,
                            paper=spec.paper,
                            orientation=spec.orientation,
                            units=spec.units,
                        )
                    except Exception:
                        strip_plan_obj = None

                strip_by_id = (
                    {s.sheet_id: s for s in strip_plan_obj.sheets}
                    if strip_plan_obj is not None
                    else {}
                )
                for idx_s, sh in enumerate(req.strip_sheets, start=1):
                    sheet_spec = spec.model_copy(
                        update={
                            "center_lat": float(sh.center_lat),
                            "center_lon": float(sh.center_lon),
                            "center_mgrs": None,
                            "title": f"{base_title} — Sheet {sh.sheet_id} ({idx_s}/{total_sheets})",
                        }
                    )
                    atlas_sheets.append((sh.sheet_id, sheet_spec))
                    matched = strip_by_id.get(sh.sheet_id)
                    if matched is not None:
                        atlas_index_sheets.append(
                            AtlasIndexSheet(
                                sheet_id=sh.sheet_id,
                                page_number=idx_s + 1,
                                center_lat=matched.center_lat,
                                center_lon=matched.center_lon,
                                center_mgrs_10m=matched.center_mgrs_10m,
                                utm_bbox=matched.utm_bbox,
                            )
                        )
                    else:
                        sh_layout = compute_sheet_layout(sheet_spec)
                        atlas_index_sheets.append(
                            AtlasIndexSheet(
                                sheet_id=sh.sheet_id,
                                page_number=idx_s + 1,
                                center_lat=float(sh.center_lat),
                                center_lon=float(sh.center_lon),
                                center_mgrs_10m=_format_mgrs_10m(
                                    float(sh.center_lat), float(sh.center_lon)
                                ),
                                utm_bbox=sh_layout.utm_bbox,
                            )
                        )
            elif outputs.pdf:
                # Re-validate native-UTM atlas projection compatibility before preparing any page.
                grid_layouts = compute_atlas_layouts(
                    compute_sheet_layout(spec), req.grid_cols, req.grid_rows
                )
                for idx_g, (sheet_id, page_layout) in enumerate(grid_layouts, start=1):
                    sheet_spec = page_layout.spec.model_copy(
                        update={"title": f"{base_title} — Sheet {sheet_id} ({idx_g}/{total_sheets})"}
                    )
                    atlas_sheets.append((sheet_id, sheet_spec))
                    atlas_index_sheets.append(
                        AtlasIndexSheet(
                            sheet_id=sheet_id,
                            page_number=idx_g + 1,
                            center_lat=page_layout.spec.center_lat,
                            center_lon=page_layout.spec.center_lon,
                            center_mgrs_10m=_format_mgrs_10m(
                                page_layout.spec.center_lat,
                                page_layout.spec.center_lon,
                            ),
                            utm_bbox=page_layout.utm_bbox,
                        )
                    )

            sheet_pdfs_dir = tmp_work_dir / "atlas_pdfs"
            sheet_pdfs_dir.mkdir(parents=True, exist_ok=True)
            saved_dems_dir = tmp_work_dir / "atlas_dems"
            saved_dems_dir.mkdir(parents=True, exist_ok=True)
            rendered_sheet_pdfs: list[Path] = []
            collected_dem_paths: list[Path] = []
            anchor_layout: SheetLayout | None = None
            anchor_declination: DeclinationInfo | None = None
            reused_anchor_workspace = None
            pdf_budget_pct = 72 if wants_any_export else 88
            need_dem_collection = bool(
                req.include_elevation_profile
                or (req.include_rf_link_card and req.rf_links)
            )

            if outputs.pdf:
                for idx, (sheet_id, sheet_spec) in enumerate(atlas_sheets, start=1):
                    if _is_cancelled(db_path, job_id):
                        return db.get_job(db_path, job_id)

                    sheet_pct = 10 + int(((idx - 1) / total_sheets) * (pdf_budget_pct - 10))
                    _report_stage(f"rendering_sheet_{sheet_id} ({idx}/{total_sheets})", sheet_pct)

                    # Queued strip sheets may carry arbitrary display labels. Give
                    # each page a filesystem identity derived only from its ordinal.
                    sheet_name = f"sheet_{idx:02d}"
                    sheet_work = tmp_work_dir / sheet_name
                    if sheet_work.resolve() != sheet_work:
                        raise ValueError("Atlas sheet workspace must be a direct child of the job workspace.")
                    sheet_work.mkdir()
                    keep_for_export = False
                    try:
                        prep_kwargs = _build_prep_kwargs(
                            req, claimed=claimed, packs_dir=packs_dir, progress_cb=None
                        )
                        sheet_ws = runner.prepare_job_workspace(
                            sheet_spec, sheet_work, **prep_kwargs
                        )
                        if anchor_layout is None and hasattr(sheet_ws, "layout"):
                            anchor_layout = sheet_ws.layout
                        if anchor_declination is None and hasattr(sheet_ws, "declination"):
                            anchor_declination = sheet_ws.declination
                        if (
                            need_dem_collection
                            and hasattr(sheet_ws, "dem_utm_tif")
                            and Path(sheet_ws.dem_utm_tif).is_file()
                        ):
                            dst_dem = saved_dems_dir / f"{sheet_name}.tif"
                            shutil.copy2(sheet_ws.dem_utm_tif, dst_dem)
                            collected_dem_paths.append(dst_dem)

                        sheet_pdf = sheet_pdfs_dir / f"{sheet_name}.pdf"
                        PyQGISRenderer().render_pdf(sheet_ws, sheet_pdf)
                        rendered_sheet_pdfs.append(sheet_pdf)

                        # Reuse matching center sheet workspace directly for digital exports
                        # instead of rebuilding a 10th workspace from scratch.
                        if (
                            wants_any_export
                            and reused_anchor_workspace is None
                            and abs(sheet_spec.center_lat - spec.center_lat) < 1e-5
                            and abs(sheet_spec.center_lon - spec.center_lon) < 1e-5
                        ):
                            reused_anchor_workspace = sheet_ws
                            keep_for_export = True
                    finally:
                        if not keep_for_export:
                            shutil.rmtree(sheet_work, ignore_errors=True)

                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)

                _report_stage("merging_atlas_pdf", pdf_budget_pct)
                atlas_name = (
                    f"{stem}_strip_{total_sheets}sheets.pdf"
                    if is_strip
                    else f"{stem}_atlas_{req.grid_cols}x{req.grid_rows}.pdf"
                )
                atlas_pdf_path = artifacts_dir / atlas_name
                if has_supplements:
                    if anchor_layout is None:
                        anchor_layout = compute_sheet_layout(spec)
                    if anchor_declination is None:
                        from gridforge.geo.declination import compute_declination

                        anchor_declination = compute_declination(anchor_layout)
                    _assemble_supplemental_pages(
                        req=req,
                        spec=spec,
                        map_pdfs=rendered_sheet_pdfs,
                        anchor_layout=anchor_layout,
                        anchor_declination=anchor_declination,
                        dem_paths=collected_dem_paths,
                        overlay_path=claimed.overlay_path,
                        strip_plan=strip_plan_obj,
                        tmp_dir=tmp_work_dir,
                        final_pdf=atlas_pdf_path,
                        atlas_index_sheets=atlas_index_sheets,
                        atlas_overlap_pct=atlas_overlap_pct,
                        atlas_title=base_title,
                    )
                else:
                    _merge_pdfs(rendered_sheet_pdfs, atlas_pdf_path)

                artifacts.append(
                    JobArtifact(
                        kind="pdf",
                        filename=atlas_name,
                        size_bytes=atlas_pdf_path.stat().st_size,
                        download_url=f"/api/jobs/{job_id}/artifacts/{atlas_name}",
                    )
                )

            anchor_workspace = reused_anchor_workspace
            if wants_any_export and anchor_workspace is None:
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

        if wants_any_export:
            assert anchor_workspace is not None
            eff_dpi = outputs.export_dpi or spec.dpi
            eff_grid = outputs.include_grid_on_digital
            shared_tif: Path | None = None
            shared_warped_3857: Path | None = None
            built_mbt_path: Path | None = None

            raster_export_count = sum(
                1
                for flag in (
                    outputs.mbtiles,
                    outputs.kmz,
                    outputs.gpkg_tiles,
                    outputs.osmand_sqlitedb,
                )
                if flag
            )
            if raster_export_count >= 2:
                _report_stage("rendering_digital_raster", 76)
                shared_tif = render_collarless_geotiff(
                    anchor_workspace,
                    tmp_work_dir / "collarless_export.tif",
                    dpi=eff_dpi,
                    include_grid=eff_grid,
                )

            if shared_tif is not None and outputs.mbtiles and outputs.gpkg_tiles:
                from gridforge.export.mbtiles import prepare_warped_3857

                shared_warped_3857 = prepare_warped_3857(
                    shared_tif,
                    tmp_work_dir / "shared_warped_3857.tif",
                    tile_format=outputs.mbtiles_format,
                    color_mode=spec.color_mode,
                    tmp_dir=tmp_work_dir,
                    step_label="gdalwarp shared EPSG:3857 for MBTiles & GPKG tiles",
                )

            if outputs.mbtiles:
                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)
                _report_stage("exporting_mbtiles", 80)
                mbt_name = f"{stem}.mbtiles"
                mbt_path = artifacts_dir / mbt_name
                export_mbtiles(
                    anchor_workspace,
                    mbt_path,
                    tile_format=outputs.mbtiles_format,
                    dpi=eff_dpi,
                    include_grid=eff_grid,
                    collarless_tif=shared_tif,
                    warped_3857_tif=shared_warped_3857,
                )
                built_mbt_path = mbt_path
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
                _report_stage("exporting_kmz", 84)
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

            if outputs.gpkg_tiles:
                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)
                _report_stage("exporting_gpkg_tiles", 88)
                gpkg_name = f"{stem}.gpkg"
                gpkg_path = artifacts_dir / gpkg_name
                export_gpkg_tiles(
                    anchor_workspace,
                    gpkg_path,
                    tile_format=outputs.mbtiles_format,
                    dpi=eff_dpi,
                    include_grid=eff_grid,
                    collarless_tif=shared_tif,
                    warped_3857_tif=shared_warped_3857,
                )
                artifacts.append(
                    JobArtifact(
                        kind="gpkg",
                        filename=gpkg_name,
                        size_bytes=gpkg_path.stat().st_size,
                        download_url=f"/api/jobs/{job_id}/artifacts/{gpkg_name}",
                    )
                )

            if outputs.osmand_sqlitedb:
                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)
                _report_stage("exporting_osmand_sqlitedb", 92)
                osm_name = f"{stem}.sqlitedb"
                osm_path = artifacts_dir / osm_name
                export_osmand_sqlitedb(
                    anchor_workspace,
                    osm_path,
                    tile_format=outputs.mbtiles_format,
                    dpi=eff_dpi,
                    include_grid=eff_grid,
                    collarless_tif=shared_tif,
                    mbtiles_path=built_mbt_path,
                )
                artifacts.append(
                    JobArtifact(
                        kind="sqlitedb",
                        filename=osm_name,
                        size_bytes=osm_path.stat().st_size,
                        download_url=f"/api/jobs/{job_id}/artifacts/{osm_name}",
                    )
                )

            if outputs.field_bundle_zip:
                if _is_cancelled(db_path, job_id):
                    return db.get_job(db_path, job_id)
                _report_stage("exporting_field_bundle", 96)
                zip_name = f"{stem}_field_bundle.zip"
                zip_path = artifacts_dir / zip_name
                bundled_paths = [
                    artifacts_dir / a.filename
                    for a in artifacts
                    if (artifacts_dir / a.filename).is_file()
                ]
                export_field_bundle_zip(
                    anchor_workspace,
                    zip_path,
                    artifacts=bundled_paths,
                )
                artifacts.append(
                    JobArtifact(
                        kind="zip",
                        filename=zip_name,
                        size_bytes=zip_path.stat().st_size,
                        download_url=f"/api/jobs/{job_id}/artifacts/{zip_name}",
                    )
                )

        if _is_cancelled(db_path, job_id):
            rec = db.get_job(db_path, job_id)
            if rec is None:
                shutil.rmtree(data_dir / job_id, ignore_errors=True)
            return rec

        elapsed = time.perf_counter() - t0
        rss_mb = _peak_rss_mb()
        completed = db.complete_job(
            db_path,
            job_id,
            artifacts=artifacts,
            elapsed_seconds=elapsed,
            peak_rss_mb=rss_mb,
        )
        if completed is None:
            shutil.rmtree(data_dir / job_id, ignore_errors=True)
        return completed
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        rss_mb = _peak_rss_mb()
        logger.exception("Job %s failed after %.2fs: %s", job_id, elapsed, exc)
        auth_mode = _resolve_auth_mode()
        if auth_mode == "hosted":
            public_error = (
                str(exc)
                if isinstance(exc, ValueError) and len(str(exc)) <= 240
                else "Map render failed due to an internal processing or upstream data error. Please try again."
            )
        else:
            public_error = str(exc)
        failed = db.fail_job(
            db_path,
            job_id,
            error=public_error,
            elapsed_seconds=elapsed,
            peak_rss_mb=rss_mb,
        )
        if failed is None:
            shutil.rmtree(data_dir / job_id, ignore_errors=True)
        return failed
    finally:
        if db.get_job(db_path, job_id) is None:
            shutil.rmtree(data_dir / job_id, ignore_errors=True)
        shutil.rmtree(tmp_work_dir, ignore_errors=True)
        db.touch_heartbeat(db_path, worker_id=worker_id, pid=os.getpid(), active_job_id=None)


DEFAULT_PRUNE_INTERVAL_SECONDS = 900.0
DEFAULT_GUEST_TTL_HOURS = 24.0
DEFAULT_FRIEND_TTL_HOURS = 336.0
DEFAULT_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_CACHE_DIR = "/tmp/gridforge-cache"
_CACHE_DISABLE_TOKENS = frozenset({"none", "unlimited", "off"})


def _env_nonempty(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    stripped = raw.strip()
    return stripped or default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())


def _resolve_auth_mode(explicit: str | None = None) -> str:
    raw = explicit if explicit is not None else os.environ.get("GRIDFORGE_AUTH_MODE", "none")
    mode = raw.strip().lower() or "none"
    if mode not in {"none", "hosted"}:
        raise ValueError(f"Invalid GRIDFORGE_AUTH_MODE={mode!r}. Expected 'none' or 'hosted'.")
    return mode


def _parse_cache_max_bytes(raw: str | int) -> int | None:
    if isinstance(raw, int):
        return raw
    cleaned = raw.strip().lower()
    if not cleaned:
        return DEFAULT_CACHE_MAX_BYTES
    if cleaned in _CACHE_DISABLE_TOKENS:
        return None
    return int(cleaned)


def _resolve_cache_max_bytes_from_env() -> int | None:
    raw = os.environ.get("GRIDFORGE_CACHE_MAX_BYTES")
    if raw is None or not raw.strip():
        return DEFAULT_CACHE_MAX_BYTES
    return _parse_cache_max_bytes(raw)


def run_pruning_pass(
    db_path: Path,
    *,
    data_dir: Path,
    cache_dir: Path | None = None,
    max_job_age_hours: float = 24.0,
    guest_ttl_hours: float | None = None,
    friend_ttl_hours: float | None = None,
    admin_ttl_hours: float | None = None,
    usage_retention_days: float | None = None,
    cache_max_bytes: int | None = None,
    auth_mode: str | None = None,
) -> dict[str, Any]:
    """Run one TTL database/artifact pruning pass and bounded LRU cache trim."""
    resolved_auth_mode = _resolve_auth_mode(auth_mode)
    use_role_ttl = (
        resolved_auth_mode == "hosted"
        or guest_ttl_hours is not None
        or friend_ttl_hours is not None
        or admin_ttl_hours is not None
    )

    eff_guest_ttl = (
        guest_ttl_hours
        if guest_ttl_hours is not None
        else _env_float("GRIDFORGE_GUEST_TTL_HOURS", DEFAULT_GUEST_TTL_HOURS)
    )
    eff_friend_ttl = (
        friend_ttl_hours
        if friend_ttl_hours is not None
        else _env_float("GRIDFORGE_FRIEND_TTL_HOURS", DEFAULT_FRIEND_TTL_HOURS)
    )
    if admin_ttl_hours is not None:
        eff_admin_ttl: float | None = admin_ttl_hours if admin_ttl_hours > 0 else None
    elif resolved_auth_mode == "hosted":
        raw_admin_ttl = _env_float("GRIDFORGE_ADMIN_TTL_HOURS", 720.0)
        eff_admin_ttl = raw_admin_ttl if raw_admin_ttl > 0 else None
    else:
        eff_admin_ttl = None

    eff_usage_days = (
        usage_retention_days
        if usage_retention_days is not None
        else _env_float("GRIDFORGE_USAGE_RETENTION_DAYS", 30.0)
    )

    if use_role_ttl:
        pruned_jobs = db.prune_expired_jobs(
            db_path,
            data_dir=data_dir,
            guest_ttl_hours=eff_guest_ttl,
            friend_ttl_hours=eff_friend_ttl,
            admin_ttl_hours=eff_admin_ttl,
        )
    else:
        pruned_jobs = db.prune_expired_jobs(
            db_path,
            data_dir=data_dir,
            max_age_hours=max_job_age_hours,
        )

    auth_pruned = db.prune_expired_projects_and_auth(
        db_path,
        guest_ttl_hours=eff_guest_ttl,
        friend_ttl_hours=eff_friend_ttl,
    )
    pruned_shares = db.prune_expired_shares(db_path)
    pruned_usage = db.prune_expired_usage_events(db_path, retention_days=eff_usage_days)

    cache_result: cache_prune.CachePruneResult | None = None
    if cache_dir is not None and cache_max_bytes is not None:
        cache_result = cache_prune.trim_cache_dir(cache_dir, cache_max_bytes)

    deleted_cache_files = cache_result.deleted_files if cache_result is not None else 0
    deleted_cache_bytes = cache_result.deleted_bytes if cache_result is not None else 0
    removed_cache_dirs = cache_result.removed_dirs if cache_result is not None else 0

    if (
        pruned_jobs
        or auth_pruned["projects"]
        or auth_pruned["sessions"]
        or auth_pruned["invites"]
        or pruned_shares
        or pruned_usage
        or deleted_cache_files
        or removed_cache_dirs
    ):
        logger.info(
            "Pruned jobs=%d projects=%d shares=%d usage=%d sessions=%d invites=%d cache_files=%d cache_bytes=%d cache_dirs=%d",
            pruned_jobs,
            auth_pruned["projects"],
            pruned_shares,
            pruned_usage,
            auth_pruned["sessions"],
            auth_pruned["invites"],
            deleted_cache_files,
            deleted_cache_bytes,
            removed_cache_dirs,
        )

    return {
        "jobs": pruned_jobs,
        "projects": auth_pruned["projects"],
        "shares": pruned_shares,
        "usage_events": pruned_usage,
        "sessions": auth_pruned["sessions"],
        "invites": auth_pruned["invites"],
        "cache_deleted_files": deleted_cache_files,
        "cache_deleted_bytes": deleted_cache_bytes,
        "cache_removed_dirs": removed_cache_dirs,
        "cache_bytes_after": cache_result.total_bytes_after if cache_result is not None else 0,
        "cache": cache_result,
    }


def run_worker_loop(
    *,
    data_dir: Path,
    packs_dir: Path,
    worker_id: str = "worker-1",
    poll_seconds: float = 1.0,
    heartbeat_seconds: float = 5.0,
    prune_interval_seconds: float | None = None,
    max_job_age_hours: float = 24.0,
    once: bool = False,
    cache_dir: Path | None = None,
    guest_ttl_hours: float | None = None,
    friend_ttl_hours: float | None = None,
    cache_max_bytes: int | None = None,
    auth_mode: str | None = None,
    disable_cache_prune: bool = False,
) -> None:
    """Run the single-concurrency SQLite worker loop with graceful shutdown."""
    db_path = data_dir / "gridforge_jobs.sqlite3"
    db.init_db(db_path)

    orphaned = db.reset_orphaned_running_jobs(db_path)
    if orphaned:
        logger.warning("Reset %d orphaned running job(s) on startup", orphaned)

    resolved_prune_interval = max(
        0.0,
        float(prune_interval_seconds)
        if prune_interval_seconds is not None
        else _env_float("GRIDFORGE_PRUNE_INTERVAL_SECONDS", DEFAULT_PRUNE_INTERVAL_SECONDS),
    )
    if cache_dir is not None:
        resolved_cache_dir: Path | None = cache_dir
    else:
        env_cache_dir = os.environ.get("GRIDFORGE_CACHE_DIR", "").strip()
        resolved_cache_dir = Path(env_cache_dir).resolve() if env_cache_dir else None

    if disable_cache_prune:
        resolved_cache_max_bytes: int | None = None
    elif cache_max_bytes is not None:
        resolved_cache_max_bytes = cache_max_bytes
    elif resolved_cache_dir is not None:
        resolved_cache_max_bytes = _resolve_cache_max_bytes_from_env()
    else:
        resolved_cache_max_bytes = None

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down GridForge worker cleanly...", signum)
        stop_event.set()

    on_main_thread = threading.current_thread() is threading.main_thread()
    prev_term = prev_int = None
    if on_main_thread:
        prev_term = signal.getsignal(signal.SIGTERM)
        prev_int = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    last_heartbeat: float | None = None
    last_prune: float | None = None
    packs_count = len(discover_packs(packs_dir))
    logger.info(
        "GridForge worker '%s' started (pid=%d, data_dir=%s, packs_dir=%s, cache_dir=%s, packs=%d, concurrency=1)",
        worker_id,
        os.getpid(),
        data_dir,
        packs_dir,
        resolved_cache_dir,
        packs_count,
    )

    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if last_heartbeat is None or (now - last_heartbeat >= heartbeat_seconds):
                db.touch_heartbeat(db_path, worker_id=worker_id, pid=os.getpid(), active_job_id=None)
                last_heartbeat = now

            if last_prune is None or (now - last_prune >= resolved_prune_interval):
                try:
                    run_pruning_pass(
                        db_path,
                        data_dir=data_dir,
                        cache_dir=resolved_cache_dir,
                        max_job_age_hours=max_job_age_hours,
                        guest_ttl_hours=guest_ttl_hours,
                        friend_ttl_hours=friend_ttl_hours,
                        cache_max_bytes=resolved_cache_max_bytes,
                        auth_mode=auth_mode,
                    )
                except Exception as exc:
                    logger.exception("Periodic pruning pass failed: %s", exc)
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
    finally:
        if on_main_thread:
            if prev_term is not None:
                signal.signal(signal.SIGTERM, prev_term)
            if prev_int is not None:
                signal.signal(signal.SIGINT, prev_int)

    logger.info("GridForge worker '%s' exited cleanly.", worker_id)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("GRIDFORGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="GridForge Phase 4 single-concurrency map worker")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--packs-dir",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=_env_float("GRIDFORGE_JOB_TTL_HOURS", 24.0),
    )
    parser.add_argument(
        "--prune-interval-seconds",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--guest-ttl-hours",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--friend-ttl-hours",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--cache-max-bytes",
        type=str,
        default=None,
    )
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    args = parser.parse_args(argv)

    raw_data_dir = args.data_dir.strip() if args.data_dir is not None else ""
    resolved_data_dir = Path(raw_data_dir or _env_nonempty("GRIDFORGE_DATA_DIR", "/data/jobs")).resolve()

    raw_packs_dir = args.packs_dir.strip() if args.packs_dir is not None else ""
    resolved_packs_dir = Path(raw_packs_dir or _env_nonempty("GRIDFORGE_PACKS_DIR", "/data/packs")).resolve()

    if args.cache_dir is None:
        resolved_cache_dir: Path | None = Path(_env_nonempty("GRIDFORGE_CACHE_DIR", DEFAULT_CACHE_DIR)).resolve()
    else:
        raw_cache_dir = args.cache_dir.strip()
        resolved_cache_dir = Path(raw_cache_dir).resolve() if raw_cache_dir else None

    raw_worker_id = args.worker_id.strip() if args.worker_id is not None else ""
    resolved_worker_id = raw_worker_id or _env_nonempty("GRIDFORGE_WORKER_ID", "worker-1")

    if args.cache_max_bytes is not None:
        eff_cache_max_bytes = _parse_cache_max_bytes(args.cache_max_bytes)
    else:
        eff_cache_max_bytes = _resolve_cache_max_bytes_from_env()

    run_worker_loop(
        data_dir=resolved_data_dir,
        packs_dir=resolved_packs_dir,
        worker_id=resolved_worker_id,
        prune_interval_seconds=args.prune_interval_seconds,
        max_job_age_hours=args.max_age_hours,
        once=args.once,
        cache_dir=resolved_cache_dir,
        guest_ttl_hours=args.guest_ttl_hours,
        friend_ttl_hours=args.friend_ttl_hours,
        cache_max_bytes=eff_cache_max_bytes,
        disable_cache_prune=(eff_cache_max_bytes is None or resolved_cache_dir is None),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
