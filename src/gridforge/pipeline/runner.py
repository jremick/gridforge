"""End-to-end GridForge pipeline orchestrator for Phases 1, 2, and 3."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from gridforge.config import (
    ColorMode,
    DemSourceKind,
    ExportFormat,
    ExportResult,
    GarminProfile,
    JobWorkspace,
    MBTilesFormat,
    RenderResult,
    SheetLayout,
    SheetSpec,
    USOverlayKind,
)
from gridforge.export.kmz import export_garmin_kmz
from gridforge.export.mbtiles import export_mbtiles
from gridforge.export.raster import render_collarless_geotiff
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
from gridforge.sources.dem_usgs3dep import AutoDEMSource, USGS3DEPSource
from gridforge.sources.osm_overpass import OverpassOSMSource
from gridforge.sources.overlays_us import build_us_overlay_sources

logger = logging.getLogger(__name__)


def _resolve_elevation_source(
    spec: SheetSpec,
    explicit_source: ElevationSource | None,
) -> ElevationSource:
    """Resolve the elevation source from an explicit override or ``spec.dem_source``."""
    if explicit_source is not None:
        return explicit_source
    if spec.dem_source == DemSourceKind.USGS3DEP:
        return USGS3DEPSource()
    if spec.dem_source == DemSourceKind.GLO30:
        return CopernicusGLO30Source()
    return AutoDEMSource()


def _merge_overlays(
    overlay_sources: Sequence[OverlaySource],
    layout: SheetLayout,
    job_dir: Path,
) -> tuple[Path | None, list[str]]:
    """Fetch and merge zero or more user overlay sources into ``job_dir / 'overlays.gpkg'``."""
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

    from osgeo import ogr

    ogr.UseExceptions()
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


def _fetch_regional_overlays(
    spec: SheetSpec,
    layout: SheetLayout,
    job_dir: Path,
    local_overrides: Mapping[str | USOverlayKind, Path | str] | None = None,
) -> tuple[dict[str, Path], list[str]]:
    """Fetch opt-in US regional overlays, retaining non-empty GeoPackages and their attributions."""
    if not spec.us_overlays:
        return {}, []

    overlay_dir = job_dir / "regional_overlays"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    sources = build_us_overlay_sources(spec.us_overlays, local_overrides=local_overrides)

    resolved_paths: dict[str, Path] = {}
    attributions: list[str] = []
    for key, src in sources.items():
        try:
            gpkg_path = src.fetch_overlay(layout, overlay_dir)
        except Exception as exc:
            logger.warning("Regional overlay '%s' failed (%s); skipping cleanly", key, exc)
            continue
        if gpkg_path is not None and Path(gpkg_path).is_file():
            resolved_paths[key] = Path(gpkg_path)
            if getattr(src, "attribution", None):
                attributions.append(src.attribution)

    return resolved_paths, attributions


def prepare_job_workspace(
    spec: SheetSpec,
    job_dir: Path | str,
    *,
    elevation_source: ElevationSource | None = None,
    vector_source: VectorSource | None = None,
    overlay_sources: Sequence[OverlaySource] | None = None,
    regional_overlay_overrides: Mapping[str | USOverlayKind, Path | str] | None = None,
) -> JobWorkspace:
    """Build a complete, render-ready ``JobWorkspace`` inside ``job_dir``.

    Shared identically by print PDF rendering (``run_render_job``) and offline
    device archive exports (``run_export_job``).
    """
    resolved_dir = Path(job_dir).resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)

    layout = compute_sheet_layout(spec)
    elev_src = _resolve_elevation_source(spec, elevation_source)
    vec_src: VectorSource = vector_source or OverpassOSMSource()

    logger.info(
        "Preparing GridForge workspace: EPSG:%s (%s) scale=1:%s paper=%s %s mode=%s dem=%s",
        layout.utm_epsg,
        layout.gzd,
        spec.scale,
        spec.paper.value,
        spec.orientation.value,
        spec.color_mode.value,
        getattr(elev_src, "name", spec.dem_source.value),
    )

    # 1. Fetch raw DEM & OSM data
    dem_raw_path = elev_src.fetch_dem(layout, resolved_dir)
    osm_raw_path = vec_src.fetch_osm(layout, resolved_dir)

    resolved_dem_name = str(
        getattr(elev_src, "resolved_source_name", None) or getattr(elev_src, "name", "unknown-dem")
    )
    resolved_vertical_datum = str(
        getattr(elev_src, "resolved_vertical_datum", None)
        or getattr(elev_src, "vertical_datum", None)
        or "EGM2008"
    )
    resolved_dem_attr = str(
        getattr(elev_src, "resolved_attribution", None) or getattr(elev_src, "attribution", "")
    )

    # 2. Normalize OSM vectors to sheet UTM GeoPackage
    osm_gpkg = normalize_osm_to_gpkg(osm_raw_path, layout, resolved_dir / "osm.gpkg")

    # 3. Build UTM DEM, multi-directional hillshade, and density-aware elevation contours
    terrain = build_terrain_products(dem_raw_path, layout, resolved_dir)

    # 4. Generate MGRS 1 km / 100 km grid & neatline ticks
    mgrs_gpkg = resolved_dir / "mgrs_grid.gpkg"
    mgrs_meta = generate_mgrs_grid(layout, mgrs_gpkg)

    # 5. Compute WMM2025 declination at sheet center elevation & render collar SVGs
    declination = compute_declination(layout, elevation_m=terrain.center_elev_m)
    declination_svg = render_declination_svg(
        declination,
        resolved_dir / "declination.svg",
        bw=(spec.color_mode == ColorMode.BW),
    )
    grid_ref_svg = render_grid_ref_box_svg(
        mgrs_meta,
        layout,
        resolved_dir / "grid_ref_box.svg",
    )

    # 6. Process optional user overlays and US regional thematic overlays
    overlays_gpkg: Path | None = None
    overlay_attributions: list[str] = []
    if overlay_sources:
        overlays_gpkg, overlay_attributions = _merge_overlays(overlay_sources, layout, resolved_dir)

    regional_overlays, regional_attributions = _fetch_regional_overlays(
        spec,
        layout,
        resolved_dir,
        local_overrides=regional_overlay_overrides,
    )

    # 7. Assemble mandatory attributions (deduplicated, preserving order)
    attributions: list[str] = []
    for attr in (
        vec_src.attribution,
        resolved_dem_attr,
        *regional_attributions,
        *overlay_attributions,
    ):
        cleaned = attr.strip() if attr else ""
        if cleaned and cleaned not in attributions:
            attributions.append(cleaned)
    if not any("GridForge" in a for a in attributions):
        attributions.append("Rendered by GridForge (GPL-3.0)")

    return JobWorkspace(
        job_dir=resolved_dir,
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
        vertical_datum=resolved_vertical_datum,
        dem_source_name=resolved_dem_name,
        regional_overlays=regional_overlays,
    )


def _run_workspace_exports(
    workspace: JobWorkspace,
    *,
    mbtiles_out: Path | None = None,
    kmz_out: Path | None = None,
    mbtiles_format: MBTilesFormat = MBTilesFormat.PNG,
    garmin_profile: GarminProfile = GarminProfile.STANDARD,
    max_tiles: int | None = None,
    include_grid: bool | None = None,
    dpi: int | None = None,
) -> list[ExportResult]:
    """Run requested MBTiles and/or Garmin KMZ exports sharing one collarless GeoTIFF render."""
    if mbtiles_out is None and kmz_out is None:
        return []

    eff_dpi = dpi if dpi is not None else workspace.layout.spec.dpi
    eff_grid = workspace.layout.spec.carto.show_grid if include_grid is None else include_grid

    shared_tif: Path | None = None
    if mbtiles_out is not None and kmz_out is not None:
        shared_tif = render_collarless_geotiff(
            workspace,
            workspace.job_dir / "collarless_export.tif",
            dpi=eff_dpi,
            include_grid=eff_grid,
        )

    results: list[ExportResult] = []
    if mbtiles_out is not None:
        results.append(
            export_mbtiles(
                workspace,
                Path(mbtiles_out),
                tile_format=mbtiles_format,
                dpi=eff_dpi,
                include_grid=eff_grid,
                collarless_tif=shared_tif,
            )
        )
    if kmz_out is not None:
        results.append(
            export_garmin_kmz(
                workspace,
                Path(kmz_out),
                profile=garmin_profile,
                max_tiles=max_tiles,
                dpi=eff_dpi,
                include_grid=eff_grid,
                collarless_tif=shared_tif,
            )
        )
    return results


def run_render_job(
    spec: SheetSpec,
    output_pdf: Path | str,
    *,
    elevation_source: ElevationSource | None = None,
    vector_source: VectorSource | None = None,
    overlay_sources: Sequence[OverlaySource] | None = None,
    regional_overlay_overrides: Mapping[str | USOverlayKind, Path | str] | None = None,
    keep_workspace: bool = False,
    work_dir: Path | str | None = None,
    renderer: MapRenderer | None = None,
    mbtiles_out: Path | str | None = None,
    kmz_out: Path | str | None = None,
    mbtiles_format: MBTilesFormat = MBTilesFormat.PNG,
    garmin_profile: GarminProfile = GarminProfile.STANDARD,
    max_tiles: int | None = None,
    export_include_grid: bool | None = None,
    export_dpi: int | None = None,
) -> RenderResult:
    """Execute the full GridForge pipeline from ``SheetSpec`` to print-ready PDF (+ optional exports)."""
    t_start = time.perf_counter()
    output_pdf_path = Path(output_pdf)
    map_renderer: MapRenderer = renderer or PyQGISRenderer()

    owns_temp_dir = work_dir is None
    if work_dir is not None:
        job_dir = Path(work_dir).resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
    else:
        job_dir = Path(tempfile.mkdtemp(prefix="gridforge_job_")).resolve()

    try:
        workspace = prepare_job_workspace(
            spec,
            job_dir,
            elevation_source=elevation_source,
            vector_source=vector_source,
            overlay_sources=overlay_sources,
            regional_overlay_overrides=regional_overlay_overrides,
        )

        # Render print-ready PDF via isolated PyQGIS subprocess
        result = map_renderer.render_pdf(workspace, output_pdf_path)

        # Optionally produce matching offline device archives from the exact same workspace
        if mbtiles_out is not None or kmz_out is not None:
            _run_workspace_exports(
                workspace,
                mbtiles_out=Path(mbtiles_out) if mbtiles_out is not None else None,
                kmz_out=Path(kmz_out) if kmz_out is not None else None,
                mbtiles_format=mbtiles_format,
                garmin_profile=garmin_profile,
                max_tiles=max_tiles,
                include_grid=export_include_grid,
                dpi=export_dpi,
            )

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


def run_export_job(
    spec: SheetSpec,
    export_format: ExportFormat,
    output_path: Path | str,
    *,
    elevation_source: ElevationSource | None = None,
    vector_source: VectorSource | None = None,
    overlay_sources: Sequence[OverlaySource] | None = None,
    regional_overlay_overrides: Mapping[str | USOverlayKind, Path | str] | None = None,
    mbtiles_format: MBTilesFormat = MBTilesFormat.PNG,
    garmin_profile: GarminProfile = GarminProfile.STANDARD,
    max_tiles: int | None = None,
    include_grid: bool | None = None,
    dpi: int | None = None,
    keep_workspace: bool = False,
    work_dir: Path | str | None = None,
) -> list[ExportResult]:
    """Export standalone ATAK ``.mbtiles`` and/or Garmin ``.kmz`` archive(s) without generating a PDF."""
    out = Path(output_path)
    mbt_out: Path | None = None
    kmz_out: Path | None = None

    if export_format == ExportFormat.MBTILES:
        mbt_out = out
    elif export_format == ExportFormat.KMZ:
        kmz_out = out
    elif export_format == ExportFormat.BOTH:
        if out.is_dir() or str(output_path).endswith(("/", "\\")):
            out.mkdir(parents=True, exist_ok=True)
            stem = (spec.title or "gridforge_export").lower().replace(" ", "_")
            mbt_out = out / f"{stem}.mbtiles"
            kmz_out = out / f"{stem}.kmz"
        else:
            mbt_out = out.with_suffix(".mbtiles")
            kmz_out = out.with_suffix(".kmz")
    else:
        raise ValueError(f"Unsupported export format: {export_format!r}")

    owns_temp_dir = work_dir is None
    if work_dir is not None:
        job_dir = Path(work_dir).resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
    else:
        job_dir = Path(tempfile.mkdtemp(prefix="gridforge_export_")).resolve()

    try:
        workspace = prepare_job_workspace(
            spec,
            job_dir,
            elevation_source=elevation_source,
            vector_source=vector_source,
            overlay_sources=overlay_sources,
            regional_overlay_overrides=regional_overlay_overrides,
        )
        return _run_workspace_exports(
            workspace,
            mbtiles_out=mbt_out,
            kmz_out=kmz_out,
            mbtiles_format=mbtiles_format,
            garmin_profile=garmin_profile,
            max_tiles=max_tiles,
            include_grid=include_grid,
            dpi=dpi,
        )
    finally:
        if owns_temp_dir and not keep_workspace:
            shutil.rmtree(job_dir, ignore_errors=True)
