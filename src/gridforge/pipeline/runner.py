"""End-to-end GridForge pipeline orchestrator."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from gridforge.config import ColorMode, JobWorkspace, RenderResult, SheetLayout, SheetSpec
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.declination import (
    compute_declination,
    render_declination_svg,
    render_grid_ref_box_svg,
)
from gridforge.geo.mgrs_grid import generate_mgrs_grid
from gridforge.pipeline.normalize import normalize_osm_to_gpkg
from gridforge.pipeline.terrain import build_terrain_products
from gridforge.render.base import MapRenderer
from gridforge.render.pyqgis_worker import PyQGISRenderer
from gridforge.sources.base import ElevationSource, OverlaySource, VectorSource
from gridforge.sources.dem_copernicus import CopernicusGLO30Source
from gridforge.sources.osm_overpass import OverpassOSMSource

logger = logging.getLogger(__name__)


def _merge_overlays(
    overlay_sources: Sequence[OverlaySource],
    layout: SheetLayout,
    job_dir: Path,
) -> tuple[Path | None, list[str]]:
    """Fetch and merge zero or more overlay sources into ``job_dir / 'overlays.gpkg'``."""
    fetched: list[Path] = []
    attributions: list[str] = []

    for idx, src in enumerate(overlay_sources):
        sub_dir = job_dir / f"overlay_src_{idx}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        path = src.fetch_overlay(layout, sub_dir)
        if path is not None and Path(path).is_file():
            fetched.append(Path(path))
            if getattr(src, "attribution", None):
                attributions.append(src.attribution)

    if not fetched:
        return None, attributions

    merged_gpkg = job_dir / "overlays.gpkg"
    if len(fetched) == 1:
        if fetched[0].resolve() != merged_gpkg.resolve():
            shutil.copy2(fetched[0], merged_gpkg)
        return merged_gpkg, attributions

    # Merge multiple overlay GeoPackages preserving unique layer names
    from osgeo import ogr

    if merged_gpkg.exists():
        merged_gpkg.unlink()

    for idx, src_gpkg in enumerate(fetched):
        ds = ogr.Open(str(src_gpkg))
        if ds is None:
            continue
        layer_names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
        ds = None

        for lname in layer_names:
            target_name = f"ov{idx}_{lname}"
            cmd = ["ogr2ogr", "-f", "GPKG"]
            if merged_gpkg.exists():
                cmd.append("-update")
            cmd.extend([str(merged_gpkg), str(src_gpkg), lname, "-nln", target_name])
            subprocess.run(cmd, check=True, capture_output=True, text=True)

    return (merged_gpkg if merged_gpkg.is_file() else None), attributions


def run_render_job(
    spec: SheetSpec,
    output_pdf: Path | str,
    *,
    elevation_source: ElevationSource | None = None,
    vector_source: VectorSource | None = None,
    overlay_sources: Sequence[OverlaySource] | None = None,
    keep_workspace: bool = False,
    work_dir: Path | str | None = None,
    renderer: MapRenderer | None = None,
) -> RenderResult:
    """Execute the full GridForge pipeline from ``SheetSpec`` to print-ready PDF."""
    t_start = time.perf_counter()
    output_pdf_path = Path(output_pdf)

    layout = compute_sheet_layout(spec)
    elev_src: ElevationSource = elevation_source or CopernicusGLO30Source()
    vec_src: VectorSource = vector_source or OverpassOSMSource()
    map_renderer: MapRenderer = renderer or PyQGISRenderer()

    owns_temp_dir = work_dir is None
    if work_dir is not None:
        job_dir = Path(work_dir).resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
    else:
        job_dir = Path(tempfile.mkdtemp(prefix="gridforge_job_")).resolve()

    try:
        logger.info(
            "Starting GridForge job: EPSG:%s (%s) scale=1:%s paper=%s %s mode=%s",
            layout.utm_epsg,
            layout.gzd,
            spec.scale,
            spec.paper.value,
            spec.orientation.value,
            spec.color_mode.value,
        )

        # 1. Fetch raw DEM & OSM data
        dem_raw_path = elev_src.fetch_dem(layout, job_dir)
        osm_raw_path = vec_src.fetch_osm(layout, job_dir)

        # 2. Normalize OSM vectors to sheet UTM GeoPackage
        osm_gpkg = normalize_osm_to_gpkg(osm_raw_path, layout, job_dir / "osm.gpkg")

        # 3. Build UTM DEM, multi-directional hillshade, and elevation contours
        terrain = build_terrain_products(dem_raw_path, layout, job_dir)

        # 4. Generate MGRS 1 km / 100 km grid & neatline ticks
        mgrs_gpkg = job_dir / "mgrs_grid.gpkg"
        mgrs_meta = generate_mgrs_grid(layout, mgrs_gpkg)

        # 5. Compute WMM2025 declination at sheet center elevation & render collar SVGs
        declination = compute_declination(layout, elevation_m=terrain.center_elev_m)
        declination_svg = render_declination_svg(
            declination,
            job_dir / "declination.svg",
            bw=(spec.color_mode == ColorMode.BW),
        )
        grid_ref_svg = render_grid_ref_box_svg(
            mgrs_meta,
            layout,
            job_dir / "grid_ref_box.svg",
        )

        # 6. Process optional user overlays
        overlays_gpkg: Path | None = None
        overlay_attributions: list[str] = []
        if overlay_sources:
            overlays_gpkg, overlay_attributions = _merge_overlays(overlay_sources, layout, job_dir)

        # 7. Assemble mandatory attributions
        attributions: list[str] = []
        for attr in (vec_src.attribution, elev_src.attribution, *overlay_attributions):
            if attr and attr.strip() and attr.strip() not in attributions:
                attributions.append(attr.strip())
        if not any("GridForge" in a for a in attributions):
            attributions.append("Rendered by GridForge (GPL-3.0)")

        workspace = JobWorkspace(
            job_dir=job_dir,
            layout=layout,
            declination=declination,
            mgrs_meta=mgrs_meta,
            osm_gpkg=osm_gpkg,
            terrain_gpkg=terrain.terrain_gpkg,
            hillshade_tif=terrain.hillshade_tif,
            dem_utm_tif=terrain.dem_utm_tif,
            mgrs_gpkg=mgrs_gpkg,
            overlays_gpkg=overlays_gpkg,
            declination_svg=declination_svg,
            grid_ref_svg=grid_ref_svg,
            contour_interval_display=terrain.contour_interval_display,
            attributions=attributions,
        )

        # 8. Render via isolated PyQGIS worker subprocess
        result = map_renderer.render_pdf(workspace, output_pdf_path)

        total_elapsed = round(time.perf_counter() - t_start, 3)
        retained_dir = job_dir if (keep_workspace or not owns_temp_dir) else None
        return result.model_copy(
            update={
                "elapsed_seconds": total_elapsed,
                "workspace_dir": retained_dir,
            }
        )
    finally:
        if owns_temp_dir and not keep_workspace:
            shutil.rmtree(job_dir, ignore_errors=True)
