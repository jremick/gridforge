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
    AU_STATE_TRAIL_OVERLAY_KINDS,
    AUOverlayKind,
    CAOverlayKind,
    CartoOptions,
    ColorMode,
    DeclinationInfo,
    DemSourceKind,
    ExportFormat,
    ExportResult,
    GarminProfile,
    JobWorkspace,
    MBTilesFormat,
    NZOverlayKind,
    Orientation,
    PackManifest,
    PaperSize,
    RenderResult,
    RFLinkSpec,
    SheetLayout,
    SheetSpec,
    TacticalGraphic,
    Units,
    USOverlayKind,
    ViewshedObserver,
    WaypointInput,
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
from gridforge.geo.strip_atlas import (
    StripAtlasPlan,
    parse_route_file,
    plan_strip_atlas,
    sample_route_elevation,
)
from gridforge.geo.supplements import (
    merge_pdf_pages,
    render_calibration_pdf,
    render_elevation_profile_pdf,
    render_route_card_pdf,
)
from gridforge.geo.waypoints import compute_route_metrics, write_waypoints_gpkg
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
from gridforge.sources.overlay_user import UserOverlaySource
from gridforge.sources.overlays_au import build_au_overlay_sources
from gridforge.sources.overlays_us import build_us_overlay_sources

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int], None]
OverlayOverrideKey = str | USOverlayKind | AUOverlayKind | NZOverlayKind | CAOverlayKind


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
    if spec.dem_source == DemSourceKind.NZ_LINZ:
        from gridforge.sources.dem_nz_linz import NZLinzDEMSource

        return NZLinzDEMSource()
    if spec.dem_source == DemSourceKind.CA_HRDEM:
        from gridforge.sources.dem_ca_hrdem import CanadaHRDEMSource

        return CanadaHRDEMSource()
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
                if isinstance(raw_key, (USOverlayKind, AUOverlayKind, NZOverlayKind, CAOverlayKind))
                else str(raw_key).lower().replace("_", "-")
            )
            merged_overrides[norm_key] = Path(raw_path).expanduser().resolve()

    # In --pack mode, keep overlays strictly air-gapped: never fall through to live HTTP.
    if spec.au_overlays:
        effective_au_kinds: list[AUOverlayKind] = []
        for kind in spec.au_overlays:
            if kind.value in merged_overrides:
                effective_au_kinds.append(kind)
            elif kind in AU_STATE_TRAIL_OVERLAY_KINDS and "au-state-trails" in merged_overrides:
                merged_overrides[kind.value] = merged_overrides["au-state-trails"]
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
    nz_kinds_override: Sequence[NZOverlayKind] | None = None,
    ca_kinds_override: Sequence[CAOverlayKind] | None = None,
) -> tuple[dict[str, Path], list[str]]:
    """Fetch opt-in US, Australia, New Zealand, and Canada regional overlays, retaining non-empty GeoPackages."""
    us_kinds = list(spec.us_overlays) if us_kinds_override is None else list(us_kinds_override)
    au_kinds = list(spec.au_overlays) if au_kinds_override is None else list(au_kinds_override)
    nz_kinds = list(spec.nz_overlays) if nz_kinds_override is None else list(nz_kinds_override)
    ca_kinds = list(spec.ca_overlays) if ca_kinds_override is None else list(ca_kinds_override)

    if not us_kinds and not au_kinds and not nz_kinds and not ca_kinds:
        return {}, []

    us_overrides: dict[str | USOverlayKind, Path | str] = {}
    au_overrides: dict[str | AUOverlayKind, Path | str] = {}
    nz_overrides: dict[str | NZOverlayKind, Path | str] = {}
    ca_overrides: dict[str | CAOverlayKind, Path | str] = {}
    if local_overrides:
        us_values = {k.value for k in USOverlayKind}
        au_values = {k.value for k in AUOverlayKind}
        nz_values = {k.value for k in NZOverlayKind}
        ca_values = {k.value for k in CAOverlayKind}
        for raw_key, raw_path in local_overrides.items():
            if isinstance(raw_key, USOverlayKind):
                us_overrides[raw_key] = raw_path
            elif isinstance(raw_key, AUOverlayKind):
                au_overrides[raw_key] = raw_path
            elif isinstance(raw_key, NZOverlayKind):
                nz_overrides[raw_key] = raw_path
            elif isinstance(raw_key, CAOverlayKind):
                ca_overrides[raw_key] = raw_path
            else:
                norm = str(raw_key).lower().replace("_", "-")
                if norm in us_values:
                    us_overrides[norm] = raw_path
                elif norm in au_values:
                    au_overrides[norm] = raw_path
                elif norm in nz_values:
                    nz_overrides[norm] = raw_path
                elif norm in ca_values:
                    ca_overrides[norm] = raw_path

    overlay_dir = job_dir / "regional_overlays"
    overlay_dir.mkdir(parents=True, exist_ok=True)

    sources: dict[str, OverlaySource] = {}
    if us_kinds:
        sources.update(build_us_overlay_sources(us_kinds, local_overrides=us_overrides or None))
    if au_kinds:
        sources.update(build_au_overlay_sources(au_kinds, local_overrides=au_overrides or None))
    if nz_kinds:
        from gridforge.sources.overlays_nz import build_nz_overlay_sources

        sources.update(build_nz_overlay_sources(nz_kinds, local_overrides=nz_overrides or None))
    if ca_kinds:
        from gridforge.sources.overlays_ca import build_ca_overlay_sources

        sources.update(build_ca_overlay_sources(ca_kinds, local_overrides=ca_overrides or None))

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
            resolved_attrs = getattr(src, "resolved_attributions", None)
            if resolved_attrs:
                for raw_attr in resolved_attrs:
                    cleaned = str(raw_attr).strip()
                    if cleaned and cleaned not in attributions:
                        attributions.append(cleaned)
            else:
                raw_attr = getattr(src, "attribution", None)
                cleaned = str(raw_attr).strip() if raw_attr else ""
                if cleaned and cleaned not in attributions:
                    attributions.append(cleaned)

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

    # 3. Build UTM DEM, multi-directional hillshade, contours, slope shading, and optional RF viewshed
    _report_progress(progress_callback, "terrain", 32)
    terrain = build_terrain_products(dem_raw_path, layout, resolved_dir)

    viewshed_tif: Path | None = getattr(terrain, "viewshed_tif", None)
    viewshed_observers_gpkg: Path | None = getattr(terrain, "viewshed_observers_gpkg", None)
    if spec.viewsheds and (viewshed_tif is None or viewshed_observers_gpkg is None):
        from gridforge.pipeline.terrain import build_viewshed_products

        viewshed_tif, viewshed_observers_gpkg = build_viewshed_products(
            terrain.dem_utm_tif,
            list(spec.viewsheds),
            layout.utm_epsg,
            spec.color_mode,
            resolved_dir,
        )

    # 4. Generate MGRS 1 km / 100 km grid & neatline ticks
    _report_progress(progress_callback, "mgrs_grid", 40)
    mgrs_gpkg = resolved_dir / "mgrs_grid.gpkg"
    mgrs_meta = generate_mgrs_grid(layout, mgrs_gpkg)

    # 5. Compute WMM2025 declination at sheet center elevation & render collar SVGs
    declination = compute_declination(layout, elevation_m=terrain.center_elev_m)
    declination_svg = render_declination_svg(
        declination,
        resolved_dir / "declination.svg",
        bw=(spec.color_mode in (ColorMode.BW, ColorMode.NIGHT_RED)),
    )
    grid_ref_svg = render_grid_ref_box_svg(
        mgrs_meta,
        layout,
        resolved_dir / "grid_ref_box.svg",
    )

    # 6. Process optional user overlays, tactical waypoints/graphics, and US/AU/NZ/CA regional overlays
    _report_progress(progress_callback, "overlays", 46)
    overlays_gpkg: Path | None = None
    overlay_attributions: list[str] = []
    if overlay_sources:
        overlays_gpkg, overlay_attributions = _merge_overlays(overlay_sources, layout, resolved_dir)

    waypoints_gpkg: Path | None = None
    if spec.waypoints:
        wp_metrics = compute_route_metrics(
            list(spec.waypoints),
            units=spec.units,
            date_iso=spec.date_iso,
            dem_path=terrain.dem_utm_tif if terrain.dem_utm_tif.is_file() else None,
        )
        waypoints_gpkg = write_waypoints_gpkg(
            wp_metrics,
            layout.utm_epsg,
            resolved_dir / "waypoints.gpkg",
        )

    tactical_graphics_gpkg: Path | None = None
    if spec.tactical_graphics or spec.rf_links:
        from gridforge.geo.tactical import write_tactical_graphics_gpkg

        tactical_graphics_gpkg = write_tactical_graphics_gpkg(
            list(spec.tactical_graphics),
            layout.utm_epsg,
            resolved_dir / "tactical_graphics.gpkg",
            rf_links=list(spec.rf_links),
            date_iso=spec.date_iso,
        )

    nz_kinds_override: list[NZOverlayKind] | None = None
    ca_kinds_override: list[CAOverlayKind] | None = None
    if pack_manifest is not None:
        nz_kinds_override = [
            k for k in spec.nz_overlays if effective_overrides and k.value in effective_overrides
        ]
        ca_kinds_override = [
            k for k in spec.ca_overlays if effective_overrides and k.value in effective_overrides
        ]

    regional_overlays, regional_attributions = _fetch_regional_overlays(
        spec,
        layout,
        resolved_dir,
        local_overrides=effective_overrides,
        us_kinds_override=us_kinds_override,
        au_kinds_override=au_kinds_override,
        nz_kinds_override=nz_kinds_override,
        ca_kinds_override=ca_kinds_override,
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
        slope_tif=terrain.slope_tif,
        waypoints_gpkg=waypoints_gpkg,
        viewshed_tif=viewshed_tif,
        viewshed_observers_gpkg=viewshed_observers_gpkg,
        tactical_graphics_gpkg=tactical_graphics_gpkg,
    )


def _run_workspace_exports(
    workspace: JobWorkspace,
    *,
    mbtiles_out: Path | None = None,
    kmz_out: Path | None = None,
    gpkg_out: Path | None = None,
    osmand_out: Path | None = None,
    field_bundle_out: Path | None = None,
    extra_bundle_artifacts: Sequence[Path | tuple[str, Path]] = (),
    mbtiles_format: MBTilesFormat = MBTilesFormat.PNG,
    garmin_profile: GarminProfile = GarminProfile.STANDARD,
    max_tiles: int | None = None,
    include_grid: bool | None = None,
    dpi: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> list[ExportResult]:
    """Run requested MBTiles, Garmin KMZ, GeoPackage tiles, OsmAnd, and Field Bundle exports."""
    raster_targets = [p for p in (mbtiles_out, kmz_out, gpkg_out, osmand_out) if p is not None]
    if not raster_targets and field_bundle_out is None:
        return []

    eff_dpi = dpi if dpi is not None else workspace.layout.spec.dpi
    eff_grid = workspace.layout.spec.carto.show_grid if include_grid is None else include_grid

    shared_tif: Path | None = None
    if len(raster_targets) >= 2:
        _report_progress(progress_callback, "rendering_raster", 82)
        shared_tif = render_collarless_geotiff(
            workspace,
            workspace.job_dir / "collarless_export.tif",
            dpi=eff_dpi,
            include_grid=eff_grid,
        )

    results: list[ExportResult] = []
    if mbtiles_out is not None:
        _report_progress(progress_callback, "exporting_mbtiles", 85)
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
        _report_progress(progress_callback, "exporting_kmz", 88)
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
    if gpkg_out is not None:
        from gridforge.export.gpkg_tiles import export_gpkg_tiles

        _report_progress(progress_callback, "exporting_gpkg", 91)
        gpkg_res = export_gpkg_tiles(
            workspace,
            Path(gpkg_out),
            tile_format=mbtiles_format,
            dpi=eff_dpi,
            include_grid=eff_grid,
            collarless_tif=shared_tif,
        )
        workspace.gpkg_tiles_path = gpkg_res.output_path
        results.append(gpkg_res)
    if osmand_out is not None:
        from gridforge.export.osmand import export_osmand_sqlitedb

        _report_progress(progress_callback, "exporting_osmand", 94)
        osm_res = export_osmand_sqlitedb(
            workspace,
            Path(osmand_out),
            tile_format=mbtiles_format,
            dpi=eff_dpi,
            include_grid=eff_grid,
            collarless_tif=shared_tif,
        )
        workspace.osmand_sqlitedb_path = osm_res.output_path
        results.append(osm_res)
    if field_bundle_out is not None:
        from gridforge.export.bundle import export_field_bundle_zip

        _report_progress(progress_callback, "exporting_bundle", 97)
        bundle_items: list[Path | tuple[str, Path]] = list(extra_bundle_artifacts)
        bundle_items.extend(res.output_path for res in results)
        bundle_res = export_field_bundle_zip(
            workspace,
            Path(field_bundle_out),
            artifacts=bundle_items,
        )
        workspace.field_bundle_zip_path = bundle_res.output_path
        results.append(bundle_res)
    return results


def _overlay_source_paths(source: OverlaySource) -> list[Path]:
    """Return resolved file paths wrapped by a user or custom overlay source."""
    raw_paths = (
        source.paths
        if isinstance(source, UserOverlaySource)
        else [getattr(source, "path", None)]
    )
    return [Path(p).expanduser().resolve() for p in raw_paths if p is not None]


def _infer_route_file_from_overlays(
    overlay_path: Path | str | None,
    overlay_sources: Sequence[OverlaySource] | None,
) -> Path | None:
    """Locate a `.gpx`/`.geojson`/`.json` route file from explicit ``overlay_path`` or user overlay sources."""
    if overlay_path is not None:
        candidate = Path(overlay_path).expanduser().resolve()
        if candidate.is_file():
            return candidate
    if overlay_sources:
        for source in overlay_sources:
            for cand in _overlay_source_paths(source):
                if cand.is_file() and cand.suffix.lower() in (".gpx", ".geojson", ".json"):
                    return cand
    return None


def _assemble_pdf_with_supplements(
    *,
    spec: SheetSpec,
    map_pdfs: list[Path],
    anchor_layout: SheetLayout,
    anchor_declination: DeclinationInfo,
    dem_paths: list[Path],
    route_file: Path | None,
    strip_plan: StripAtlasPlan | None,
    job_dir: Path,
    output_pdf: Path,
    overlap_pct: float = 0.0,
) -> Path:
    """Assemble ``[Atlas Index?] + [Elevation Profile?] + Map Sheet(s) + [Route Card?] + [RF Link Card?] + [100 mm Calibration?]``."""
    bw = spec.color_mode in (ColorMode.BW, ColorMode.NIGHT_RED)
    supp_dir = job_dir / "supplements"
    supp_dir.mkdir(parents=True, exist_ok=True)
    front_pages: list[Path] = []
    back_pages: list[Path] = []

    parsed_route_lonlat: list[tuple[float, float]] | None = None
    if route_file is not None and route_file.is_file():
        try:
            parsed_route_lonlat = parse_route_file(route_file)
        except Exception as exc:
            logger.warning("Could not parse '%s' as route (%s)", route_file, exc)

    if spec.include_elevation_profile and dem_paths:
        route_lonlat = parsed_route_lonlat
        if route_lonlat is None and len(spec.waypoints) >= 2:
            route_lonlat = [(w.lon, w.lat) for w in spec.waypoints]
        if route_lonlat is None and strip_plan is not None and len(strip_plan.sheets) >= 2:
            route_lonlat = [(s.center_lon, s.center_lat) for s in strip_plan.sheets]

        if route_lonlat is not None and len(route_lonlat) >= 2:
            profile = sample_route_elevation(route_lonlat, dem_paths, anchor_layout.utm_epsg)
            elev_pdf = render_elevation_profile_pdf(
                profile,
                strip_plan,
                anchor_layout,
                supp_dir / "elevation_profile.pdf",
                bw=bw,
            )
            front_pages.append(elev_pdf)

    if spec.include_atlas_index and strip_plan is not None and strip_plan.sheets:
        from pyproj import Transformer

        from gridforge.geo.supplements import AtlasIndexSheet, render_atlas_index_pdf

        first_sheet_page = 2 + len(front_pages)
        index_sheets = [
            AtlasIndexSheet(
                sheet_id=sh.sheet_id,
                page_number=first_sheet_page + idx_s,
                center_lat=sh.center_lat,
                center_lon=sh.center_lon,
                center_mgrs_10m=sh.center_mgrs_10m,
                utm_bbox=sh.utm_bbox,
            )
            for idx_s, sh in enumerate(strip_plan.sheets)
        ]
        route_utm: list[tuple[float, float]] | None = None
        if parsed_route_lonlat and len(parsed_route_lonlat) >= 2:
            to_utm = Transformer.from_crs(
                "EPSG:4326", f"EPSG:{strip_plan.utm_epsg}", always_xy=True
            )
            rxs, rys = to_utm.transform(
                [p[0] for p in parsed_route_lonlat],
                [p[1] for p in parsed_route_lonlat],
            )
            route_utm = [(float(x), float(y)) for x, y in zip(rxs, rys)]

        idx_pdf = render_atlas_index_pdf(
            index_sheets,
            page_width_mm=anchor_layout.page_width_mm,
            page_height_mm=anchor_layout.page_height_mm,
            scale=spec.scale,
            utm_epsg=strip_plan.utm_epsg,
            title=(spec.title or "GRIDFORGE STRIP ATLAS"),
            overlap_pct=overlap_pct,
            route_utm=route_utm,
            output_pdf=supp_dir / "atlas_index.pdf",
            bw=bw,
        )
        front_pages.insert(0, idx_pdf)

    if spec.include_route_card and spec.waypoints:
        dem_for_wp = dem_paths[0] if dem_paths else None
        metrics = compute_route_metrics(
            list(spec.waypoints),
            units=spec.units,
            date_iso=spec.date_iso,
            dem_path=dem_for_wp,
        )
        card_pdf = render_route_card_pdf(
            metrics,
            anchor_layout,
            anchor_declination,
            supp_dir / "route_card.pdf",
            bw=bw,
        )
        back_pages.append(card_pdf)

    if spec.include_rf_link_card and spec.rf_links and dem_paths:
        from gridforge.geo.supplements import render_rf_link_card_pdf
        from gridforge.pipeline.terrain import compute_rf_link_profile

        rf_profiles = [
            compute_rf_link_profile(
                dem_paths[0],
                lnk,
                anchor_layout.utm_epsg,
                date_iso=spec.date_iso,
            )
            for lnk in spec.rf_links
        ]
        rf_pdf = render_rf_link_card_pdf(
            rf_profiles,
            anchor_layout,
            supp_dir / "rf_link_card.pdf",
            bw=bw,
        )
        back_pages.append(rf_pdf)

    if spec.include_calibration_page:
        cal_pdf = render_calibration_pdf(
            anchor_layout.page_width_mm,
            anchor_layout.page_height_mm,
            spec.scale,
            supp_dir / "calibration.pdf",
            bw=bw,
        )
        back_pages.append(cal_pdf)

    all_pages = [*front_pages, *map_pdfs, *back_pages]
    if len(all_pages) == 1 and all_pages[0].resolve() == output_pdf.resolve():
        return output_pdf
    return merge_pdf_pages(all_pages, output_pdf)


def run_render_job(
    spec: SheetSpec,
    output_pdf: Path | str,
    *,
    elevation_source: ElevationSource | None = None,
    vector_source: VectorSource | None = None,
    overlay_sources: Sequence[OverlaySource] | None = None,
    overlay_path: Path | str | None = None,
    regional_overlay_overrides: Mapping[OverlayOverrideKey, Path | str] | None = None,
    pack_dir: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
    keep_workspace: bool = False,
    work_dir: Path | str | None = None,
    renderer: MapRenderer | None = None,
    mbtiles_out: Path | str | None = None,
    kmz_out: Path | str | None = None,
    gpkg_out: Path | str | None = None,
    osmand_out: Path | str | None = None,
    field_bundle_out: Path | str | None = None,
    mbtiles_format: MBTilesFormat = MBTilesFormat.PNG,
    garmin_profile: GarminProfile = GarminProfile.STANDARD,
    max_tiles: int | None = None,
    export_include_grid: bool | None = None,
    export_dpi: int | None = None,
) -> RenderResult:
    """Execute the full GridForge pipeline from ``SheetSpec`` to print-ready PDF (+ optional supplements/exports)."""
    t_start = time.perf_counter()
    output_pdf_path = Path(output_pdf)
    map_renderer: MapRenderer = renderer or PyQGISRenderer()

    combined_overlays: list[OverlaySource] = list(overlay_sources or [])
    if overlay_path is not None:
        resolved_ov = Path(overlay_path).expanduser().resolve()
        existing_paths = {p for s in combined_overlays for p in _overlay_source_paths(s)}
        if resolved_ov not in existing_paths:
            combined_overlays.insert(0, UserOverlaySource(resolved_ov))

    route_file = _infer_route_file_from_overlays(overlay_path, combined_overlays)
    wants_supplements = bool(
        spec.include_calibration_page
        or (spec.include_route_card and len(spec.waypoints) > 0)
        or (spec.include_rf_link_card and len(spec.rf_links) > 0)
        or spec.include_elevation_profile
    )

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
            overlay_sources=combined_overlays or None,
            regional_overlay_overrides=regional_overlay_overrides,
            pack_dir=pack_dir,
            progress_callback=progress_callback,
        )

        # Render print-ready PDF via isolated PyQGIS subprocess
        _report_progress(progress_callback, "rendering_pdf", 65)
        if wants_supplements:
            raw_map_pdf = job_dir / "map_sheet_01.pdf"
            result = map_renderer.render_pdf(workspace, raw_map_pdf)
            _report_progress(progress_callback, "rendering_supplements", 78)
            dem_paths = [workspace.dem_utm_tif] if workspace.dem_utm_tif.is_file() else []
            _assemble_pdf_with_supplements(
                spec=spec,
                map_pdfs=[raw_map_pdf],
                anchor_layout=workspace.layout,
                anchor_declination=workspace.declination,
                dem_paths=dem_paths,
                route_file=route_file,
                strip_plan=None,
                job_dir=job_dir,
                output_pdf=output_pdf_path,
            )
            result = result.model_copy(update={"output_pdf": output_pdf_path})
        else:
            result = map_renderer.render_pdf(workspace, output_pdf_path)

        # Optionally produce matching offline device archives / Field Bundle from the exact same workspace
        if any(x is not None for x in (mbtiles_out, kmz_out, gpkg_out, osmand_out, field_bundle_out)):
            _run_workspace_exports(
                workspace,
                mbtiles_out=Path(mbtiles_out) if mbtiles_out is not None else None,
                kmz_out=Path(kmz_out) if kmz_out is not None else None,
                gpkg_out=Path(gpkg_out) if gpkg_out is not None else None,
                osmand_out=Path(osmand_out) if osmand_out is not None else None,
                field_bundle_out=Path(field_bundle_out) if field_bundle_out is not None else None,
                extra_bundle_artifacts=[output_pdf_path],
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


def run_strip_atlas_job(
    route_file: Path | str,
    output_pdf: Path | str,
    *,
    scale: int = 25000,
    paper: PaperSize = PaperSize.A4,
    orientation: Orientation = Orientation.PORTRAIT,
    color_mode: ColorMode = ColorMode.COLOR,
    units: Units = Units.METRIC,
    title: str | None = None,
    date_iso: str | None = None,
    geopdf: bool = False,
    dpi: int = 300,
    dem_source: DemSourceKind = DemSourceKind.AUTO,
    us_overlays: Sequence[USOverlayKind] | None = None,
    au_overlays: Sequence[AUOverlayKind] | None = None,
    nz_overlays: Sequence[NZOverlayKind] | None = None,
    ca_overlays: Sequence[CAOverlayKind] | None = None,
    carto: CartoOptions | None = None,
    waypoints: Sequence[WaypointInput] | None = None,
    viewsheds: Sequence[ViewshedObserver] | None = None,
    rf_links: Sequence[RFLinkSpec] | None = None,
    tactical_graphics: Sequence[TacticalGraphic] | None = None,
    include_calibration_page: bool = False,
    include_route_card: bool = False,
    include_elevation_profile: bool = True,
    include_atlas_index: bool = False,
    include_rf_link_card: bool = False,
    show_fold_marks: bool = False,
    overlap_pct: float = 10.0,
    max_sheets: int = 12,
    elevation_source: ElevationSource | None = None,
    vector_source: VectorSource | None = None,
    overlay_sources: Sequence[OverlaySource] | None = None,
    regional_overlay_overrides: Mapping[OverlayOverrideKey, Path | str] | None = None,
    pack_dir: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
    keep_workspace: bool = False,
    work_dir: Path | str | None = None,
    renderer: MapRenderer | None = None,
) -> RenderResult:
    """Plan and sequentially render a minimum-count GPX/GeoJSON Strip Atlas PDF (+ supplements)."""
    t_start = time.perf_counter()
    resolved_route = Path(route_file).expanduser().resolve()
    output_pdf_path = Path(output_pdf)
    map_renderer: MapRenderer = renderer or PyQGISRenderer()

    route_lonlat = parse_route_file(resolved_route)
    strip_plan = plan_strip_atlas(
        route_lonlat,
        scale=scale,
        paper=paper,
        orientation=orientation,
        units=units,
        overlap_pct=overlap_pct,
        max_sheets=max_sheets,
    )

    combined_overlays: list[OverlaySource] = [UserOverlaySource(resolved_route)]
    for src in overlay_sources or []:
        if _overlay_source_paths(src) == [resolved_route]:
            continue
        combined_overlays.append(src)

    base_title = (title.strip() if title else resolved_route.stem.replace("_", " ").replace("-", " ").upper())
    total_sheets = len(strip_plan.sheets)
    eff_carto = carto if carto is not None else CartoOptions()
    eff_waypoints = list(waypoints or [])
    eff_viewsheds = list(viewsheds or [])
    eff_rf_links = list(rf_links or [])
    eff_tactical = list(tactical_graphics or [])
    eff_us = list(dict.fromkeys(us_overlays or []))
    eff_au = list(dict.fromkeys(au_overlays or []))
    eff_nz = list(dict.fromkeys(nz_overlays or []))
    eff_ca = list(dict.fromkeys(ca_overlays or []))

    owns_temp_dir = work_dir is None
    if work_dir is not None:
        job_dir = Path(work_dir).resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
    else:
        job_dir = Path(tempfile.mkdtemp(prefix="gridforge_strip_")).resolve()

    try:
        sheet_pdfs_dir = job_dir / "strip_pdfs"
        sheet_pdfs_dir.mkdir(parents=True, exist_ok=True)
        saved_dems_dir = job_dir / "strip_dems"
        saved_dems_dir.mkdir(parents=True, exist_ok=True)

        rendered_sheet_pdfs: list[Path] = []
        collected_dem_paths: list[Path] = []
        anchor_layout: SheetLayout | None = None
        anchor_declination: DeclinationInfo | None = None
        anchor_spec: SheetSpec | None = None

        for idx, sh in enumerate(strip_plan.sheets, start=1):
            sheet_pct = 10 + int(((idx - 1) / max(1, total_sheets)) * 70)
            _report_progress(progress_callback, f"rendering_sheet_{sh.sheet_id}", sheet_pct)

            sheet_title = (
                f"{base_title} — Sheet {sh.sheet_id} ({idx}/{total_sheets})"
                if total_sheets > 1
                else base_title
            )
            sheet_spec = SheetSpec(
                center_lat=sh.center_lat,
                center_lon=sh.center_lon,
                center_mgrs=sh.center_mgrs_10m,
                scale=scale,
                paper=paper,
                orientation=orientation,
                color_mode=color_mode,
                units=units,
                title=sheet_title,
                date_iso=date_iso,
                geopdf=geopdf,
                dpi=dpi,
                carto=eff_carto,
                dem_source=dem_source,
                us_overlays=eff_us,
                au_overlays=eff_au,
                nz_overlays=eff_nz,
                ca_overlays=eff_ca,
                waypoints=eff_waypoints,
                viewsheds=eff_viewsheds,
                rf_links=eff_rf_links,
                tactical_graphics=eff_tactical,
                include_calibration_page=include_calibration_page,
                include_route_card=include_route_card,
                include_elevation_profile=include_elevation_profile,
                include_atlas_index=include_atlas_index,
                include_rf_link_card=include_rf_link_card,
                show_fold_marks=show_fold_marks,
            )
            if anchor_spec is None:
                anchor_spec = sheet_spec.model_copy(update={"title": base_title})

            sheet_work = job_dir / f"sheet_{idx:02d}_{sh.sheet_id}"
            sheet_work.mkdir(parents=True, exist_ok=True)
            try:
                ws = prepare_job_workspace(
                    sheet_spec,
                    sheet_work,
                    elevation_source=elevation_source,
                    vector_source=vector_source,
                    overlay_sources=combined_overlays,
                    regional_overlay_overrides=regional_overlay_overrides,
                    pack_dir=pack_dir,
                )
                if anchor_layout is None:
                    anchor_layout = ws.layout
                if anchor_declination is None:
                    anchor_declination = ws.declination
                if include_elevation_profile and ws.dem_utm_tif.is_file():
                    dem_copy = saved_dems_dir / f"dem_{idx:02d}_{sh.sheet_id}.tif"
                    shutil.copy2(ws.dem_utm_tif, dem_copy)
                    collected_dem_paths.append(dem_copy)

                sheet_pdf = sheet_pdfs_dir / f"sheet_{idx:02d}_{sh.sheet_id}.pdf"
                map_renderer.render_pdf(ws, sheet_pdf)
                rendered_sheet_pdfs.append(sheet_pdf)
            finally:
                if not keep_workspace:
                    shutil.rmtree(sheet_work, ignore_errors=True)

        assert anchor_layout is not None and anchor_declination is not None and anchor_spec is not None
        _report_progress(progress_callback, "rendering_supplements", 88)
        _assemble_pdf_with_supplements(
            spec=anchor_spec,
            map_pdfs=rendered_sheet_pdfs,
            anchor_layout=anchor_layout,
            anchor_declination=anchor_declination,
            dem_paths=collected_dem_paths,
            route_file=resolved_route,
            strip_plan=strip_plan,
            job_dir=job_dir,
            output_pdf=output_pdf_path,
            overlap_pct=overlap_pct,
        )

        _report_progress(progress_callback, "done", 100)
        total_elapsed = round(time.perf_counter() - t_start, 3)
        retained_dir = job_dir if (keep_workspace or not owns_temp_dir) else None
        return RenderResult(
            output_pdf=output_pdf_path,
            page_width_mm=anchor_layout.page_width_mm,
            page_height_mm=anchor_layout.page_height_mm,
            scale=scale,
            utm_epsg=strip_plan.utm_epsg,
            elapsed_seconds=total_elapsed,
            workspace_dir=retained_dir,
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
    """Export standalone ATAK ``.mbtiles``, Garmin ``.kmz``, GeoPackage ``.gpkg``, OsmAnd ``.sqlitedb``, or Field Bundle ``.zip``."""
    out = Path(output_path)
    mbt_out: Path | None = None
    kmz_out: Path | None = None
    gpkg_out: Path | None = None
    osmand_out: Path | None = None
    bundle_out: Path | None = None

    if export_format == ExportFormat.MBTILES:
        mbt_out = out
    elif export_format == ExportFormat.KMZ:
        kmz_out = out
    elif export_format == ExportFormat.GPKG:
        gpkg_out = out
    elif export_format == ExportFormat.OSMAND:
        osmand_out = out
    elif export_format == ExportFormat.BUNDLE:
        bundle_out = out
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
            gpkg_out=gpkg_out,
            osmand_out=osmand_out,
            field_bundle_out=bundle_out,
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
