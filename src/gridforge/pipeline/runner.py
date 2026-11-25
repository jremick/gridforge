"""End-to-end GridForge pipeline orchestrator for Phases 1–4 and Australia-wide mapping."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from gridforge.config import (
    AUOverlayKind,
    ColorMode,
    DemSourceKind,
    ExportFormat,
    ExportResult,
    GarminProfile,
    JobWorkspace,
    MBTilesFormat,
    PackManifest,
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
from gridforge.sources.dem_au_elvis import AUElvisDEMSource
from gridforge.sources.dem_copernicus import CopernicusGLO30Source
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.dem_usgs3dep import AutoDEMSource, USGS3DEPSource
from gridforge.sources.osm_overpass import OverpassOSMSource
from gridforge.sources.osm_pbf import LocalPBFSource
from gridforge.sources.overlays_au import build_au_overlay_sources
from gridforge.sources.overlays_us import build_us_overlay_sources

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int], None]
OverlayOverrideKey = str | USOverlayKind | AUOverlayKind


def _report_progress(callback: ProgressCallback | None, stage: str, pct: int) -> None:
    """Invoke optional progress callback with clamped percentage (propagating cancellation)."""
    if callback is None:
        return
    callback(stage, max(0, min(100, int(pct))))


def _resolve_elevation_source(
    spec: SheetSpec,
    explicit_source: ElevationSource | None,
) -> ElevationSource:
    """Resolve the elevation source from an explicit override or ``spec.dem_source``."""
    if explicit_source is not None:
        return explicit_source
    if spec.dem_source == DemSourceKind.USGS3DEP:
        return USGS3DEPSource()
    if spec.dem_source == DemSourceKind.AU_ELVIS:
        return AUElvisDEMSource()
    if spec.dem_source == DemSourceKind.GLO30:
        return CopernicusGLO30Source()
    return AutoDEMSource()


def _bboxes_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def _resolve_pack_sources(
    pack_dir: Path | str,
    spec: SheetSpec,
    layout: SheetLayout,
    *,
    elevation_source: ElevationSource | None,
    vector_source: VectorSource | None,
    regional_overlay_overrides: Mapping[OverlayOverrideKey, Path | str] | None,
) -> tuple[
    PackManifest,
    ElevationSource,
    bool,
    VectorSource,
    dict[str, Path],
    list[USOverlayKind],
    list[AUOverlayKind],
]:
    """Load an offline regional pack and wire air-gapped DEM, PBF, and overlay GPKGs."""
    from gridforge.packs.builder import load_pack_manifest

    resolved_pack_dir = Path(pack_dir).expanduser().resolve()
    manifest = load_pack_manifest(resolved_pack_dir)

    if not _bboxes_intersect(layout.wgs84_bbox.as_tuple(), manifest.bbox_wgs84.as_tuple()):
        logger.warning(
            "Requested sheet footprint %s does not intersect offline pack '%s' bounds %s",
            layout.wgs84_bbox.as_tuple(),
            manifest.pack_id,
            manifest.bbox_wgs84.as_tuple(),
        )

    pack_used_dem = False
    resolved_elev = elevation_source
    if resolved_elev is None:
        if not manifest.dem_path:
            raise ValueError(
                f"Offline pack '{manifest.pack_id}' has no DEM. "
                "Choose a pack with elevation data or supply --offline-dem."
            )
        dem_file = (resolved_pack_dir / manifest.dem_path).resolve()
        if not dem_file.exists():
            raise FileNotFoundError(
                f"Offline pack '{manifest.pack_id}' DEM not found at '{dem_file}'"
            )
        resolved_elev = LocalDEMSource(dem_file, attribution=manifest.dem_attribution)
        pack_used_dem = True

    resolved_vec = vector_source
    if resolved_vec is None:
        if not manifest.osm_pbf_path:
            raise ValueError(
                f"Offline pack '{manifest.pack_id}' has no OSM data. "
                "Choose a pack with OSM data or supply --offline-pbf."
            )
        pbf_file = (resolved_pack_dir / manifest.osm_pbf_path).resolve()
        if not pbf_file.is_file():
            raise FileNotFoundError(
                f"Offline pack '{manifest.pack_id}' OSM PBF not found at '{pbf_file}'"
            )
        resolved_vec = LocalPBFSource(pbf_file)

    merged_overrides: dict[str, Path] = {}
    pack_au_kinds: list[AUOverlayKind] = []
    for raw_key, rel_path in manifest.overlays.items():
        norm_key = str(raw_key).lower().replace("_", "-")
        ov_path = (resolved_pack_dir / rel_path).resolve()
        if not ov_path.is_file():
            logger.warning(
                "Offline pack '%s' overlay '%s' missing at '%s'; skipping",
                manifest.pack_id,
                norm_key,
                ov_path,
            )
            continue
        merged_overrides[norm_key] = ov_path
        try:
            au_kind = AUOverlayKind(norm_key)
        except ValueError:
            continue
        if au_kind not in pack_au_kinds:
            pack_au_kinds.append(au_kind)

    if regional_overlay_overrides:
        for raw_key, raw_path in regional_overlay_overrides.items():
            norm_key = (
                raw_key.value
                if isinstance(raw_key, (USOverlayKind, AUOverlayKind))
                else str(raw_key).lower().replace("_", "-")
            )
            merged_overrides[norm_key] = Path(raw_path).expanduser().resolve()

    # In --pack mode, keep overlays strictly air-gapped: never fall through to live HTTP.
    if spec.au_overlays:
        effective_au_kinds: list[AUOverlayKind] = []
        for kind in spec.au_overlays:
            if kind.value in merged_overrides:
                effective_au_kinds.append(kind)
            else:
                logger.warning(
                    "Requested AU overlay '%s' is not bundled in offline pack '%s'; skipping in air-gapped mode",
                    kind.value,
                    manifest.pack_id,
                )
    else:
        effective_au_kinds = pack_au_kinds

    effective_us_kinds: list[USOverlayKind] = []
    for kind in spec.us_overlays:
        if kind.value in merged_overrides:
            effective_us_kinds.append(kind)
        else:
            logger.warning(
                "Requested US overlay '%s' is not bundled in offline pack '%s'; skipping in air-gapped mode",
                kind.value,
                manifest.pack_id,
            )

    return (
        manifest,
        resolved_elev,
        pack_used_dem,
        resolved_vec,
        merged_overrides,
        effective_us_kinds,
        effective_au_kinds,
    )


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
    local_overrides: Mapping[OverlayOverrideKey, Path | str] | None = None,
    *,
    us_kinds_override: Sequence[USOverlayKind] | None = None,
    au_kinds_override: Sequence[AUOverlayKind] | None = None,
) -> tuple[dict[str, Path], list[str]]:
    """Fetch opt-in US and Australia regional overlays, retaining non-empty GeoPackages."""
    us_kinds = list(spec.us_overlays) if us_kinds_override is None else list(us_kinds_override)
    au_kinds = list(spec.au_overlays) if au_kinds_override is None else list(au_kinds_override)

    if not us_kinds and not au_kinds:
        return {}, []

    us_overrides: dict[str | USOverlayKind, Path | str] = {}
    au_overrides: dict[str | AUOverlayKind, Path | str] = {}
    if local_overrides:
        us_values = {k.value for k in USOverlayKind}
        au_values = {k.value for k in AUOverlayKind}
        for raw_key, raw_path in local_overrides.items():
            if isinstance(raw_key, USOverlayKind):
                us_overrides[raw_key] = raw_path
            elif isinstance(raw_key, AUOverlayKind):
                au_overrides[raw_key] = raw_path
            else:
                norm = str(raw_key).lower().replace("_", "-")
                if norm in us_values:
                    us_overrides[norm] = raw_path
                elif norm in au_values:
                    au_overrides[norm] = raw_path

    overlay_dir = job_dir / "regional_overlays"
    overlay_dir.mkdir(parents=True, exist_ok=True)

    sources: dict[str, OverlaySource] = {}
    if us_kinds:
        sources.update(build_us_overlay_sources(us_kinds, local_overrides=us_overrides or None))
    if au_kinds:
        sources.update(build_au_overlay_sources(au_kinds, local_overrides=au_overrides or None))

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
    regional_overlay_overrides: Mapping[OverlayOverrideKey, Path | str] | None = None,
    pack_dir: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> JobWorkspace:
    """Build a complete, render-ready ``JobWorkspace`` inside ``job_dir``.

    Shared identically by print PDF rendering (``run_render_job``) and offline
    device archive exports (``run_export_job``).
    """
    resolved_dir = Path(job_dir).resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)

    layout = compute_sheet_layout(spec)

    pack_manifest: PackManifest | None = None
    pack_used_dem = False
    effective_overrides: Mapping[OverlayOverrideKey, Path | str] | None = regional_overlay_overrides
    us_kinds_override: list[USOverlayKind] | None = None
    au_kinds_override: list[AUOverlayKind] | None = None

    if pack_dir is not None:
        (
            pack_manifest,
            elev_src,
            pack_used_dem,
            vec_src,
            effective_overrides,
            us_kinds_override,
            au_kinds_override,
        ) = _resolve_pack_sources(
            pack_dir,
            spec,
            layout,
            elevation_source=elevation_source,
            vector_source=vector_source,
            regional_overlay_overrides=regional_overlay_overrides,
        )
    else:
        elev_src = _resolve_elevation_source(spec, elevation_source)
        vec_src = vector_source or OverpassOSMSource()

    logger.info(
        "Preparing GridForge workspace: EPSG:%s (%s) scale=1:%s paper=%s %s mode=%s dem=%s%s",
        layout.utm_epsg,
        layout.gzd,
        spec.scale,
        spec.paper.value,
        spec.orientation.value,
        spec.color_mode.value,
        getattr(elev_src, "name", spec.dem_source.value),
        f" pack={pack_manifest.pack_id}" if pack_manifest is not None else "",
    )

    # 1. Fetch raw DEM & OSM data
    _report_progress(progress_callback, "fetching_data", 15)
    dem_raw_path = elev_src.fetch_dem(layout, resolved_dir)
    osm_raw_path = vec_src.fetch_osm(layout, resolved_dir)

    if pack_used_dem and pack_manifest is not None:
        resolved_dem_name = f"pack:{pack_manifest.pack_id}"
        resolved_vertical_datum = pack_manifest.vertical_datum or "AHD"
        resolved_dem_attr = str(
            pack_manifest.dem_attribution or getattr(elev_src, "attribution", "")
        )
    else:
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
    _report_progress(progress_callback, "normalizing_osm", 24)
    osm_gpkg = normalize_osm_to_gpkg(osm_raw_path, layout, resolved_dir / "osm.gpkg")

    # 3. Build UTM DEM, multi-directional hillshade, and density-aware elevation contours
    _report_progress(progress_callback, "terrain", 32)
    terrain = build_terrain_products(dem_raw_path, layout, resolved_dir)

    # 4. Generate MGRS 1 km / 100 km grid & neatline ticks
    _report_progress(progress_callback, "mgrs_grid", 40)
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

    # 6. Process optional user overlays and US/AU regional thematic overlays
    _report_progress(progress_callback, "overlays", 46)
    overlays_gpkg: Path | None = None
    overlay_attributions: list[str] = []
    if overlay_sources:
        overlays_gpkg, overlay_attributions = _merge_overlays(overlay_sources, layout, resolved_dir)

    regional_overlays, regional_attributions = _fetch_regional_overlays(
        spec,
        layout,
        resolved_dir,
        local_overrides=effective_overrides,
        us_kinds_override=us_kinds_override,
        au_kinds_override=au_kinds_override,
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
    progress_callback: ProgressCallback | None = None,
) -> list[ExportResult]:
    """Run requested MBTiles and/or Garmin KMZ exports sharing one collarless GeoTIFF render."""
    if mbtiles_out is None and kmz_out is None:
        return []

    eff_dpi = dpi if dpi is not None else workspace.layout.spec.dpi
    eff_grid = workspace.layout.spec.carto.show_grid if include_grid is None else include_grid

    shared_tif: Path | None = None
    if mbtiles_out is not None and kmz_out is not None:
        _report_progress(progress_callback, "rendering_raster", 84)
        shared_tif = render_collarless_geotiff(
            workspace,
            workspace.job_dir / "collarless_export.tif",
            dpi=eff_dpi,
            include_grid=eff_grid,
        )

    results: list[ExportResult] = []
    if mbtiles_out is not None:
        _report_progress(progress_callback, "exporting_mbtiles", 88)
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
        _report_progress(progress_callback, "exporting_kmz", 94)
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
    regional_overlay_overrides: Mapping[OverlayOverrideKey, Path | str] | None = None,
    pack_dir: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
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
            pack_dir=pack_dir,
            progress_callback=progress_callback,
        )

        # Render print-ready PDF via isolated PyQGIS subprocess
        _report_progress(progress_callback, "rendering_pdf", 75)
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
                progress_callback=progress_callback,
            )

        _report_progress(progress_callback, "done", 100)
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
    regional_overlay_overrides: Mapping[OverlayOverrideKey, Path | str] | None = None,
    pack_dir: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
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
            pack_dir=pack_dir,
            progress_callback=progress_callback,
        )
        results = _run_workspace_exports(
            workspace,
            mbtiles_out=mbt_out,
            kmz_out=kmz_out,
            mbtiles_format=mbtiles_format,
            garmin_profile=garmin_profile,
            max_tiles=max_tiles,
            include_grid=include_grid,
            dpi=dpi,
            progress_callback=progress_callback,
        )
        _report_progress(progress_callback, "done", 100)
        return results
    finally:
        if owns_temp_dir and not keep_workspace:
            shutil.rmtree(job_dir, ignore_errors=True)
