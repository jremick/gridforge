"""GridForge Typer CLI (``render``, ``export``, ``inspect``, ``pack``, and ``serve``)."""

from __future__ import annotations

from enum import Enum
import json
import logging
import os
from pathlib import Path
from typing import Literal, Optional, cast

import typer

from gridforge.config import (
    VALID_SCALES,
    AUOverlayKind,
    CAOverlayKind,
    CartoOptions,
    ColorMode,
    DemSourceKind,
    DensityLevel,
    ExportFormat,
    GarminProfile,
    HillshadeStrength,
    MBTilesFormat,
    NZOverlayKind,
    Orientation,
    PaperSize,
    RFLinkSpec,
    SheetSpec,
    TacticalGraphic,
    Units,
    USOverlayKind,
    ViewshedObserver,
    WaypointCategory,
    WaypointInput,
)
from gridforge.geo.crs import compute_sheet_layout, parse_center
from gridforge.geo.declination import compute_declination
from gridforge.packs.builder import resolve_pack_directory
from gridforge.pipeline.runner import run_export_job, run_render_job, run_strip_atlas_job
from gridforge.sources.base import ElevationSource, OverlaySource, VectorSource
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource
from gridforge.sources.overlay_user import UserOverlaySource

app = typer.Typer(
    name="gridforge",
    help="GridForge — Military-style true-scale MGRS topographic map generator, offline pack builder & web server.",
    no_args_is_help=True,
)
pack_app = typer.Typer(
    name="pack",
    help="Build and inspect air-gapped offline regional data packs (Australia states & tactical regions).",
    no_args_is_help=True,
)
app.add_typer(pack_app, name="pack")


class PackDemTier(str, Enum):
    """Supported DEM tiers when building an offline regional pack."""

    AUTO = "auto"
    SRTM_1S = "srtm-1s"
    GLO30 = "glo30"
    AU_ELVIS = "au-elvis"
    NZ_LINZ = "nz-linz"
    USGS3DEP = "usgs3dep"
    CA_HRDEM = "ca-hrdem"


def _format_bytes(num_bytes: int) -> str:
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{num_bytes} B"


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _cli_pack(path: Path | None, pack_id: str | None) -> Path | None:
    """Resolve mutually exclusive ``--pack`` directory or installed ``--pack-id``."""
    if path is not None and pack_id is not None:
        raise typer.BadParameter("Choose either --pack or --pack-id.")
    if pack_id is None:
        return path
    packs_root = os.environ.get("GRIDFORGE_PACKS_DIR", "/data/packs")
    try:
        return resolve_pack_directory(packs_root, pack_id)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--pack-id") from exc


def _parse_waypoints(raw_waypoints: list[str] | None) -> list[WaypointInput]:
    """Parse repeatable ``--waypoint 'Label,lat,lon[,category[,notes]]'`` CLI strings."""
    parsed: list[WaypointInput] = []
    for raw in raw_waypoints or []:
        parts = [p.strip() for p in raw.split(",", maxsplit=4)]
        if len(parts) < 3:
            raise typer.BadParameter(
                f"Invalid --waypoint '{raw}'. Expected 'Label,lat,lon[,category[,notes]]'."
            )
        label = parts[0]
        if not label:
            raise typer.BadParameter(f"Waypoint label must be non-empty in '{raw}'.")
        try:
            lat = float(parts[1])
            lon = float(parts[2])
        except ValueError as exc:
            raise typer.BadParameter(
                f"Invalid waypoint coordinates in '{raw}'. Expected decimal degrees lat,lon."
            ) from exc

        cat = WaypointCategory.WAYPOINT
        if len(parts) >= 4 and parts[3]:
            try:
                cat = WaypointCategory(parts[3].lower())
            except ValueError as exc:
                allowed = ", ".join(c.value for c in WaypointCategory)
                raise typer.BadParameter(
                    f"Invalid waypoint category '{parts[3]}' in '{raw}'. Choose: {allowed}."
                ) from exc

        notes = parts[4] if len(parts) == 5 and parts[4] else None
        try:
            parsed.append(WaypointInput(label=label, lat=lat, lon=lon, category=cat, notes=notes))
        except Exception as exc:
            raise typer.BadParameter(f"Invalid waypoint '{raw}': {exc}") from exc
    return parsed


def _parse_viewsheds(raw_viewsheds: list[str] | None) -> list[ViewshedObserver]:
    """Parse repeatable ``--viewshed 'lat,lon[,obs_m[,tgt_m[,max_km[,label]]]]'`` CLI strings."""
    parsed: list[ViewshedObserver] = []
    for idx, raw in enumerate(raw_viewsheds or [], start=1):
        parts = [p.strip() for p in raw.split(",", maxsplit=5)]
        if len(parts) < 2:
            raise typer.BadParameter(
                f"Invalid --viewshed '{raw}'. Expected 'lat,lon[,obs_m[,tgt_m[,max_km[,label]]]]'."
            )
        try:
            lat = float(parts[0])
            lon = float(parts[1])
            obs_m = float(parts[2]) if len(parts) >= 3 and parts[2] else 2.0
            tgt_m = float(parts[3]) if len(parts) >= 4 and parts[3] else 1.5
            max_km = float(parts[4]) if len(parts) >= 5 and parts[4] else 10.0
        except ValueError as exc:
            raise typer.BadParameter(
                f"Invalid numeric field in --viewshed '{raw}'. Expected 'lat,lon[,obs_m[,tgt_m[,max_km[,label]]]]'."
            ) from exc

        label = parts[5] if len(parts) == 6 else ""
        if not label:
            label = f"Observer {idx}"

        try:
            parsed.append(
                ViewshedObserver(
                    label=label,
                    lat=lat,
                    lon=lon,
                    observer_height_m=obs_m,
                    target_height_m=tgt_m,
                    max_distance_m=max_km * 1000.0,
                )
            )
        except Exception as exc:
            raise typer.BadParameter(f"Invalid --viewshed '{raw}': {exc}") from exc
    return parsed


def _parse_rf_links(raw_links: list[str] | None) -> list[RFLinkSpec]:
    """Parse repeatable ``--rf-link 'from_lat,from_lon,to_lat,to_lon[,freq_mhz[,from_h_m[,to_h_m[,from_label[,to_label]]]]]'``."""
    parsed: list[RFLinkSpec] = []
    for idx, raw in enumerate(raw_links or [], start=1):
        parts = [p.strip() for p in raw.split(",", maxsplit=8)]
        if len(parts) < 4:
            raise typer.BadParameter(
                f"Invalid --rf-link '{raw}'. Expected "
                "'from_lat,from_lon,to_lat,to_lon[,freq_mhz[,from_h_m[,to_h_m[,from_label[,to_label]]]]]'"
            )
        try:
            from_lat = float(parts[0])
            from_lon = float(parts[1])
            to_lat = float(parts[2])
            to_lon = float(parts[3])
            freq_mhz = float(parts[4]) if len(parts) >= 5 and parts[4] else 915.0
            from_h = float(parts[5]) if len(parts) >= 6 and parts[5] else 2.0
            to_h = float(parts[6]) if len(parts) >= 7 and parts[6] else 2.0
        except ValueError as exc:
            raise typer.BadParameter(
                f"Invalid numeric field in --rf-link '{raw}'."
            ) from exc

        from_label = parts[7] if len(parts) >= 8 and parts[7] else f"TX{idx}"
        to_label = parts[8] if len(parts) >= 9 and parts[8] else f"RX{idx}"
        try:
            parsed.append(
                RFLinkSpec(
                    from_lat=from_lat,
                    from_lon=from_lon,
                    to_lat=to_lat,
                    to_lon=to_lon,
                    freq_mhz=freq_mhz,
                    from_height_m=from_h,
                    to_height_m=to_h,
                    from_label=from_label,
                    to_label=to_label,
                )
            )
        except Exception as exc:
            raise typer.BadParameter(f"Invalid --rf-link '{raw}': {exc}") from exc
    return parsed


def _parse_range_rings(raw_rings: list[str] | None) -> list[TacticalGraphic]:
    """Parse repeatable ``--range-ring 'lat,lon[,r1/r2/r3[,label]]'`` CLI strings."""
    parsed: list[TacticalGraphic] = []
    for raw in raw_rings or []:
        parts = [p.strip() for p in raw.split(",", maxsplit=3)]
        if len(parts) < 2:
            raise typer.BadParameter(
                f"Invalid --range-ring '{raw}'. Expected 'lat,lon[,r1/r2/r3[,label]]'."
            )
        try:
            lat = float(parts[0])
            lon = float(parts[1])
        except ValueError as exc:
            raise typer.BadParameter(f"Invalid coordinates in --range-ring '{raw}'.") from exc

        radii_km: list[float] = [1.0, 3.0, 5.0]
        if len(parts) >= 3 and parts[2]:
            tokens = [
                t.strip()
                for t in parts[2].replace(":", "/").replace(";", "/").replace("|", "/").split("/")
                if t.strip()
            ]
            try:
                radii_km = [float(t) for t in tokens]
            except ValueError as exc:
                raise typer.BadParameter(
                    f"Invalid radii '{parts[2]}' in --range-ring '{raw}'. Use e.g. '1:3:5' or '1/3/5'."
                ) from exc
            if not radii_km:
                raise typer.BadParameter(f"At least one positive radius is required in --range-ring '{raw}'.")

        label = parts[3] if len(parts) >= 4 else ""
        try:
            parsed.append(
                TacticalGraphic(
                    kind="range_ring",
                    label=label,
                    points=[(lat, lon)],
                    radii_km=radii_km,
                )
            )
        except Exception as exc:
            raise typer.BadParameter(f"Invalid --range-ring '{raw}': {exc}") from exc
    return parsed


def _parse_tactical_lines(raw_lines: list[str] | None) -> list[TacticalGraphic]:
    """Parse repeatable ``--tactical-line 'lat1,lon1,lat2,lon2[,label]'`` or ``'lat1,lon1;lat2,lon2[;...][,label]'``."""
    parsed: list[TacticalGraphic] = []
    for raw in raw_lines or []:
        pts: list[tuple[float, float]] = []
        label = ""
        if ";" in raw:
            segs = [s.strip() for s in raw.split(";") if s.strip()]
            for idx_s, seg in enumerate(segs):
                sp = [p.strip() for p in seg.split(",", maxsplit=2)]
                if len(sp) < 2:
                    raise typer.BadParameter(
                        f"Invalid --tactical-line '{raw}'. Expected 'lat1,lon1,lat2,lon2[,label]' or 'lat1,lon1;lat2,lon2[,label]'."
                    )
                try:
                    pts.append((float(sp[0]), float(sp[1])))
                except ValueError as exc:
                    raise typer.BadParameter(f"Invalid coordinate in --tactical-line '{raw}'.") from exc
                if idx_s == len(segs) - 1 and len(sp) >= 3:
                    label = sp[2]
        else:
            parts = [p.strip() for p in raw.split(",", maxsplit=4)]
            if len(parts) < 4:
                raise typer.BadParameter(
                    f"Invalid --tactical-line '{raw}'. Expected 'lat1,lon1,lat2,lon2[,label]'."
                )
            try:
                pts = [(float(parts[0]), float(parts[1])), (float(parts[2]), float(parts[3]))]
            except ValueError as exc:
                raise typer.BadParameter(f"Invalid coordinates in --tactical-line '{raw}'.") from exc
            if len(parts) >= 5:
                label = parts[4]

        if len(pts) < 2:
            raise typer.BadParameter(f"--tactical-line '{raw}' requires at least 2 coordinate pairs.")
        try:
            parsed.append(TacticalGraphic(kind="bearing_line", label=label, points=pts))
        except Exception as exc:
            raise typer.BadParameter(f"Invalid --tactical-line '{raw}': {exc}") from exc
    return parsed


def _build_spec(
    *,
    center: str,
    scale: int,
    paper: PaperSize,
    orientation: Orientation,
    mode: ColorMode,
    units: Units,
    title: str | None = None,
    date_iso: str | None = None,
    geopdf: bool = False,
    dpi: int = 300,
    dem_source: DemSourceKind = DemSourceKind.AUTO,
    us_overlays: list[USOverlayKind] | None = None,
    au_overlays: list[AUOverlayKind] | None = None,
    nz_overlays: list[NZOverlayKind] | None = None,
    ca_overlays: list[CAOverlayKind] | None = None,
    waypoints: list[WaypointInput] | None = None,
    viewsheds: list[ViewshedObserver] | None = None,
    rf_links: list[RFLinkSpec] | None = None,
    tactical_graphics: list[TacticalGraphic] | None = None,
    include_calibration_page: bool = False,
    include_route_card: bool = False,
    include_elevation_profile: bool = False,
    include_atlas_index: bool = False,
    include_rf_link_card: bool = False,
    show_fold_marks: bool = False,
    carto: CartoOptions | None = None,
) -> SheetSpec:
    if scale not in VALID_SCALES:
        allowed = ", ".join(str(s) for s in VALID_SCALES)
        raise typer.BadParameter(f"Invalid scale {scale}. Choose one of: {allowed}")

    try:
        lat, lon, canonical_mgrs = parse_center(center)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    return SheetSpec(
        center_lat=lat,
        center_lon=lon,
        center_mgrs=canonical_mgrs,
        scale=scale,
        paper=paper,
        orientation=orientation,
        color_mode=mode,
        units=units,
        title=title,
        date_iso=date_iso,
        geopdf=geopdf,
        dpi=dpi,
        dem_source=dem_source,
        us_overlays=list(dict.fromkeys(us_overlays or [])),
        au_overlays=list(dict.fromkeys(au_overlays or [])),
        nz_overlays=list(dict.fromkeys(nz_overlays or [])),
        ca_overlays=list(dict.fromkeys(ca_overlays or [])),
        waypoints=list(waypoints or []),
        viewsheds=list(viewsheds or []),
        rf_links=list(rf_links or []),
        tactical_graphics=list(tactical_graphics or []),
        include_calibration_page=include_calibration_page,
        include_route_card=include_route_card,
        include_elevation_profile=include_elevation_profile,
        include_atlas_index=include_atlas_index,
        include_rf_link_card=include_rf_link_card,
        show_fold_marks=show_fold_marks,
        carto=carto if carto is not None else CartoOptions(),
    )


@app.command("render")
def render_cmd(
    center: Optional[str] = typer.Option(
        None,
        "--center",
        "-c",
        help='Center coordinate as "lat,lon" (e.g. "-33.732,150.312") or MGRS string (omit when using --strip-route).',
    ),
    strip_route: Optional[Path] = typer.Option(
        None,
        "--strip-route",
        help="GPX or GeoJSON route file to auto-fit a minimum-count multi-page true-scale Strip Atlas PDF.",
    ),
    overlap_pct: float = typer.Option(
        10.0,
        "--overlap-pct",
        min=0.0,
        max=40.0,
        help="Edge margin / overlap percentage for --strip-route sheets (default 10%).",
    ),
    scale: int = typer.Option(
        25000,
        "--scale",
        "-s",
        help="Map scale denominator (15000, 24000, 25000, 48000, 50000).",
    ),
    paper: PaperSize = typer.Option(
        PaperSize.A4,
        "--paper",
        "-p",
        case_sensitive=False,
        help="Physical paper size (letter, a4, legal, a3).",
    ),
    orientation: Orientation = typer.Option(
        Orientation.PORTRAIT,
        "--orientation",
        "-o",
        case_sensitive=False,
        help="Page orientation (portrait, landscape).",
    ),
    mode: ColorMode = typer.Option(
        ColorMode.COLOR,
        "--mode",
        "--color-mode",
        "-m",
        case_sensitive=False,
        help="Cartographic theme (color, bw, night-red, alpine).",
    ),
    units: Units = typer.Option(
        Units.METRIC,
        "--units",
        "-u",
        case_sensitive=False,
        help="Contour & elevation units (metric, imperial).",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-O",
        help="Destination PDF file path.",
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        "-t",
        help="Optional sheet header title (defaults to center MGRS).",
    ),
    geopdf: bool = typer.Option(
        False,
        "--geopdf/--no-geopdf",
        help="Embed GeoPDF spatial reference metadata.",
    ),
    dpi: int = typer.Option(
        300,
        "--dpi",
        min=72,
        max=1200,
        help="Export DPI (default 300).",
    ),
    dem_source: DemSourceKind = typer.Option(
        DemSourceKind.AUTO,
        "--dem-source",
        case_sensitive=False,
        help="Elevation source selection (auto, glo30, usgs3dep, au-elvis, nz-linz, ca-hrdem).",
    ),
    us_overlay: Optional[list[USOverlayKind]] = typer.Option(
        None,
        "--us-overlay",
        case_sensitive=False,
        help="Optional US public-domain overlay (fema, nhd, towers, enc, us-trails, us-public-lands); repeatable.",
    ),
    au_overlay: Optional[list[AUOverlayKind]] = typer.Option(
        None,
        "--au-overlay",
        case_sensitive=False,
        help=(
            "Optional Australia CC BY 4.0 overlay (au-hydro, au-parks, au-infra, au-aviation, "
            "au-state-trails [auto all 8 states/territories], au-nsw-trails, au-vic-trails, "
            "au-tas-trails, au-qld-trails, au-wa-trails, au-sa-trails, au-nt-trails, au-act-trails); repeatable."
        ),
    ),
    nz_overlay: Optional[list[NZOverlayKind]] = typer.Option(
        None,
        "--nz-overlay",
        case_sensitive=False,
        help="Optional New Zealand CC BY 4.0 overlay (nz-doc-trails); repeatable.",
    ),
    ca_overlay: Optional[list[CAOverlayKind]] = typer.Option(
        None,
        "--ca-overlay",
        case_sensitive=False,
        help="Optional Canada OGL-Canada overlay (ca-canvec-trails); repeatable.",
    ),
    pack: Optional[Path] = typer.Option(
        None,
        "--pack",
        help="Path to an air-gapped offline regional pack directory containing pack.json.",
    ),
    pack_id: Optional[str] = typer.Option(
        None,
        "--pack-id",
        help="Installed offline regional pack ID resolved under GRIDFORGE_PACKS_DIR (default /data/packs).",
    ),
    contour_density: DensityLevel = typer.Option(
        DensityLevel.NORMAL,
        "--contour-density",
        case_sensitive=False,
        help="Contour interval density (low, normal, high).",
    ),
    hillshade_strength: HillshadeStrength = typer.Option(
        HillshadeStrength.NORMAL,
        "--hillshade-strength",
        case_sensitive=False,
        help="Multi-directional hillshade intensity (off, light, normal, strong).",
    ),
    label_density: DensityLevel = typer.Option(
        DensityLevel.NORMAL,
        "--label-density",
        case_sensitive=False,
        help="Cartographic label density (low, normal, high).",
    ),
    buildings: bool = typer.Option(True, "--buildings/--no-buildings", help="Show building footprints & usage icons."),
    trees: bool = typer.Option(True, "--trees/--no-trees", help="Show evergreen/deciduous/mixed tree cover."),
    power: bool = typer.Option(True, "--power/--no-power", help="Show power lines, towers, and substations."),
    pipelines: bool = typer.Option(True, "--pipelines/--no-pipelines", help="Show pipelines."),
    grid: bool = typer.Option(True, "--grid/--no-grid", help="Show MGRS 1 km grid & neatline ticks."),
    export_grid: bool = typer.Option(
        True,
        "--export-grid/--no-export-grid",
        help="Include the enabled print grid in accompanying digital exports.",
    ),
    river_arrows: bool = typer.Option(
        True,
        "--river-arrows/--no-river-arrows",
        help="Show directional downstream flow arrows on rivers & streams.",
    ),
    slope_shading: bool = typer.Option(
        False,
        "--slope-shading/--no-slope-shading",
        help="Overlay backcountry slope-angle avalanche shading (>27 deg Color / >30 deg B&W).",
    ),
    waypoint: Optional[list[str]] = typer.Option(
        None,
        "--waypoint",
        help='Add a numbered tactical waypoint as "Label,lat,lon[,category]" (waypoint|camp|water|hazard|lz); repeatable.',
    ),
    viewshed: Optional[list[str]] = typer.Option(
        None,
        "--viewshed",
        help='Add an RF/LOS viewshed observer as "lat,lon[,obs_m[,tgt_m[,max_km[,label]]]]"; repeatable.',
    ),
    rf_link: Optional[list[str]] = typer.Option(
        None,
        "--rf-link",
        help='Add a point-to-point RF Fresnel link as "from_lat,from_lon,to_lat,to_lon[,freq_mhz[,from_h[,to_h[,from_lbl[,to_lbl]]]]]"; repeatable.',
    ),
    range_ring: Optional[list[str]] = typer.Option(
        None,
        "--range-ring",
        help='Add concentric tactical range rings as "lat,lon[,r1/r2/r3[,label]]" (km); repeatable.',
    ),
    tactical_line: Optional[list[str]] = typer.Option(
        None,
        "--tactical-line",
        help='Add a tactical bearing line as "lat1,lon1,lat2,lon2[,label]"; repeatable.',
    ),
    calibration_page: bool = typer.Option(
        False,
        "--calibration-page/--no-calibration-page",
        help="Append an exact 100.00 mm Printer Scale Verification & MGRS Romer page to the PDF.",
    ),
    route_card: bool = typer.Option(
        False,
        "--route-card/--no-route-card",
        help="Append a Tactical Route Card page (MGRS, leg distances, Grid & WMM2025 Magnetic azimuths).",
    ),
    elevation_profile: bool = typer.Option(
        False,
        "--elevation-profile/--no-elevation-profile",
        help="Prepend a Route Overview & DEM Elevation Profile page (from --strip-route, --overlay, or --waypoint).",
    ),
    atlas_index: bool = typer.Option(
        False,
        "--atlas-index/--no-atlas-index",
        help="Prepend a vector Atlas Overview & Sheet Index cover page for multi-sheet / strip atlases.",
    ),
    rf_link_card: bool = typer.Option(
        False,
        "--rf-link-card/--no-rf-link-card",
        help="Append a curved-Earth RF / Fresnel Link Budget & Profile Card page for --rf-link links.",
    ),
    fold_marks: bool = typer.Option(
        False,
        "--fold-marks/--no-fold-marks",
        help="Draw 4.0 mm corner crop & margin fold/join registration marks outside the neatline.",
    ),
    mbtiles: Optional[Path] = typer.Option(
        None,
        "--mbtiles",
        help="Also export an ATAK-ready MBTiles archive of the exact same map area.",
    ),
    kmz: Optional[Path] = typer.Option(
        None,
        "--kmz",
        help="Also export a Garmin Custom Maps KMZ archive of the exact same map area.",
    ),
    gpkg: Optional[Path] = typer.Option(
        None,
        "--gpkg",
        help="Also export an OGC GeoPackage (.gpkg) raster tile pyramid of the exact same map area.",
    ),
    osmand: Optional[Path] = typer.Option(
        None,
        "--osmand",
        help="Also export an OsmAnd (.sqlitedb) offline raster map archive of the exact same map area.",
    ),
    field_bundle: Optional[Path] = typer.Option(
        None,
        "--field-bundle",
        help="Also package a Field Bundle (.zip) with PDF, requested device archives, GPX 1.1, and README.",
    ),
    garmin_profile: GarminProfile = typer.Option(
        GarminProfile.STANDARD,
        "--garmin-profile",
        case_sensitive=False,
        help="Garmin KMZ tile cap profile (standard=100 tiles, modern=500 tiles).",
    ),
    max_tiles: Optional[int] = typer.Option(
        None,
        "--max-tiles",
        min=1,
        help="Override Garmin KMZ maximum tile count.",
    ),
    mbtiles_format: MBTilesFormat = typer.Option(
        MBTilesFormat.PNG,
        "--mbtiles-format",
        case_sensitive=False,
        help="MBTiles tile encoding format (png, jpg).",
    ),
    offline_dem: Optional[Path] = typer.Option(
        None,
        "--offline-dem",
        help="Local GeoTIFF/VRT or directory of DEM tiles for air-gapped rendering.",
    ),
    offline_pbf: Optional[Path] = typer.Option(
        None,
        "--offline-pbf",
        help="Local .osm.pbf or .osm XML file for air-gapped rendering.",
    ),
    overlay: Optional[list[Path]] = typer.Option(
        None,
        "--overlay",
        help="Optional user overlay file (.geojson, .gpx, .csv); repeatable.",
    ),
    date_iso: Optional[str] = typer.Option(
        None,
        "--date",
        help="ISO date (YYYY-MM-DD) for WMM2025 magnetic declination.",
    ),
    keep_workspace: bool = typer.Option(
        False,
        "--keep-workspace/--no-keep-workspace",
        help="Keep intermediate workspace GeoPackages, hillshade, and SVGs.",
    ),
    work_dir: Optional[Path] = typer.Option(
        None,
        "--work-dir",
        help="Explicit intermediate workspace directory (implies retaining files).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json/--no-json",
        help="Print RenderResult as JSON.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable debug logging.",
    ),
) -> None:
    """Render a print-ready, physically true-scale MGRS topographic PDF (+ optional strip atlas/supplements/exports)."""
    _configure_logging(verbose)

    if strip_route is None and not center:
        raise typer.BadParameter("Provide either --center or --strip-route.")

    carto = CartoOptions(
        contour_density=contour_density,
        hillshade_strength=hillshade_strength,
        label_density=label_density,
        show_buildings=buildings,
        show_trees=trees,
        show_power=power,
        show_pipelines=pipelines,
        show_grid=grid,
        show_river_arrows=river_arrows,
        show_slope_shading=slope_shading,
    )
    parsed_waypoints = _parse_waypoints(waypoint)
    parsed_viewsheds = _parse_viewsheds(viewshed)
    parsed_rf_links = _parse_rf_links(rf_link)
    parsed_tactical = [
        *_parse_range_rings(range_ring),
        *_parse_tactical_lines(tactical_line),
    ]

    elev_src: ElevationSource | None = LocalDEMSource(offline_dem) if offline_dem is not None else None
    vec_src: VectorSource | None = LocalPBFSource(offline_pbf) if offline_pbf is not None else None
    overlay_srcs: list[OverlaySource] = [UserOverlaySource(p) for p in (overlay or [])]
    pack_dir = _cli_pack(pack, pack_id)

    if strip_route is not None:
        if scale not in VALID_SCALES:
            allowed = ", ".join(str(s) for s in VALID_SCALES)
            raise typer.BadParameter(f"Invalid scale {scale}. Choose one of: {allowed}")
        result = run_strip_atlas_job(
            strip_route,
            output,
            scale=scale,
            paper=paper,
            orientation=orientation,
            color_mode=mode,
            units=units,
            title=title,
            date_iso=date_iso,
            geopdf=geopdf,
            dpi=dpi,
            dem_source=dem_source,
            us_overlays=us_overlay,
            au_overlays=au_overlay,
            nz_overlays=nz_overlay,
            ca_overlays=ca_overlay,
            carto=carto,
            waypoints=parsed_waypoints,
            viewsheds=parsed_viewsheds,
            rf_links=parsed_rf_links,
            tactical_graphics=parsed_tactical,
            include_calibration_page=calibration_page,
            include_route_card=route_card,
            include_elevation_profile=elevation_profile,
            include_atlas_index=atlas_index,
            include_rf_link_card=rf_link_card,
            show_fold_marks=fold_marks,
            overlap_pct=overlap_pct,
            elevation_source=elev_src,
            vector_source=vec_src,
            overlay_sources=overlay_srcs or None,
            pack_dir=pack_dir,
            keep_workspace=keep_workspace,
            work_dir=work_dir,
        )
    else:
        assert center is not None
        spec = _build_spec(
            center=center,
            scale=scale,
            paper=paper,
            orientation=orientation,
            mode=mode,
            units=units,
            title=title,
            date_iso=date_iso,
            geopdf=geopdf,
            dpi=dpi,
            dem_source=dem_source,
            us_overlays=us_overlay,
            au_overlays=au_overlay,
            nz_overlays=nz_overlay,
            ca_overlays=ca_overlay,
            waypoints=parsed_waypoints,
            viewsheds=parsed_viewsheds,
            rf_links=parsed_rf_links,
            tactical_graphics=parsed_tactical,
            include_calibration_page=calibration_page,
            include_route_card=route_card,
            include_elevation_profile=elevation_profile,
            include_atlas_index=atlas_index,
            include_rf_link_card=rf_link_card,
            show_fold_marks=fold_marks,
            carto=carto,
        )
        result = run_render_job(
            spec,
            output,
            elevation_source=elev_src,
            vector_source=vec_src,
            overlay_sources=overlay_srcs or None,
            pack_dir=pack_dir,
            keep_workspace=keep_workspace,
            work_dir=work_dir,
            mbtiles_out=mbtiles,
            kmz_out=kmz,
            gpkg_out=gpkg,
            osmand_out=osmand,
            field_bundle_out=field_bundle,
            mbtiles_format=mbtiles_format,
            garmin_profile=garmin_profile,
            max_tiles=max_tiles,
            export_include_grid=grid and export_grid,
            export_dpi=dpi,
        )

    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(
            f"Rendered {result.output_pdf} "
            f"({result.page_width_mm:.1f}x{result.page_height_mm:.1f} mm, "
            f"1:{result.scale:,}, EPSG:{result.utm_epsg}) in {result.elapsed_seconds:.2f}s"
        )
        if mbtiles is not None:
            typer.echo(f"Exported ATAK MBTiles: {mbtiles}")
        if kmz is not None:
            typer.echo(f"Exported Garmin KMZ: {kmz}")
        if gpkg is not None:
            typer.echo(f"Exported GeoPackage Raster Tiles: {gpkg}")
        if osmand is not None:
            typer.echo(f"Exported OsmAnd SQLiteDB: {osmand}")
        if field_bundle is not None:
            typer.echo(f"Exported Field Bundle ZIP: {field_bundle}")
        if result.workspace_dir is not None:
            typer.echo(f"Workspace retained at: {result.workspace_dir}")


@app.command("export")
def export_cmd(
    center: str = typer.Option(
        ...,
        "--center",
        "-c",
        help='Center coordinate as "lat,lon" (e.g. "-33.732,150.312") or MGRS string.',
    ),
    scale: int = typer.Option(
        25000,
        "--scale",
        "-s",
        help="Map scale denominator (15000, 24000, 25000, 48000, 50000).",
    ),
    paper: PaperSize = typer.Option(
        PaperSize.A4,
        "--paper",
        "-p",
        case_sensitive=False,
        help="Physical sheet footprint size (letter, a4, legal, a3).",
    ),
    orientation: Orientation = typer.Option(
        Orientation.PORTRAIT,
        "--orientation",
        "-o",
        case_sensitive=False,
        help="Sheet footprint orientation (portrait, landscape).",
    ),
    mode: ColorMode = typer.Option(
        ColorMode.COLOR,
        "--mode",
        "--color-mode",
        "-m",
        case_sensitive=False,
        help="Cartographic theme (color, bw, night-red, alpine).",
    ),
    units: Units = typer.Option(
        Units.METRIC,
        "--units",
        "-u",
        case_sensitive=False,
        help="Contour & elevation units (metric, imperial).",
    ),
    export_format: ExportFormat = typer.Option(
        ExportFormat.MBTILES,
        "--format",
        "-f",
        case_sensitive=False,
        help="Offline device archive format (mbtiles, kmz, both, gpkg, osmand, bundle).",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-O",
        help="Destination file path (or output prefix/directory when --format both).",
    ),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Optional map name/title metadata."),
    dpi: int = typer.Option(300, "--dpi", min=72, max=1200, help="Collarless rasterization DPI (default 300)."),
    garmin_profile: GarminProfile = typer.Option(
        GarminProfile.STANDARD,
        "--garmin-profile",
        case_sensitive=False,
        help="Garmin KMZ tile cap profile (standard=100 tiles, modern=500 tiles).",
    ),
    max_tiles: Optional[int] = typer.Option(
        None,
        "--max-tiles",
        min=1,
        help="Override Garmin KMZ maximum tile count.",
    ),
    mbtiles_format: MBTilesFormat = typer.Option(
        MBTilesFormat.PNG,
        "--mbtiles-format",
        case_sensitive=False,
        help="MBTiles tile encoding format (png, jpg).",
    ),
    dem_source: DemSourceKind = typer.Option(
        DemSourceKind.AUTO,
        "--dem-source",
        case_sensitive=False,
        help="Elevation source selection (auto, glo30, usgs3dep, au-elvis, nz-linz, ca-hrdem).",
    ),
    us_overlay: Optional[list[USOverlayKind]] = typer.Option(
        None,
        "--us-overlay",
        case_sensitive=False,
        help="Optional US public-domain overlay (fema, nhd, towers, enc, us-trails, us-public-lands); repeatable.",
    ),
    au_overlay: Optional[list[AUOverlayKind]] = typer.Option(
        None,
        "--au-overlay",
        case_sensitive=False,
        help=(
            "Optional Australia CC BY 4.0 overlay (au-hydro, au-parks, au-infra, au-aviation, "
            "au-state-trails [auto all 8 states/territories], au-nsw-trails, au-vic-trails, "
            "au-tas-trails, au-qld-trails, au-wa-trails, au-sa-trails, au-nt-trails, au-act-trails); repeatable."
        ),
    ),
    nz_overlay: Optional[list[NZOverlayKind]] = typer.Option(
        None,
        "--nz-overlay",
        case_sensitive=False,
        help="Optional New Zealand CC BY 4.0 overlay (nz-doc-trails); repeatable.",
    ),
    ca_overlay: Optional[list[CAOverlayKind]] = typer.Option(
        None,
        "--ca-overlay",
        case_sensitive=False,
        help="Optional Canada OGL-Canada overlay (ca-canvec-trails); repeatable.",
    ),
    pack: Optional[Path] = typer.Option(
        None,
        "--pack",
        help="Path to an air-gapped offline regional pack directory containing pack.json.",
    ),
    pack_id: Optional[str] = typer.Option(
        None,
        "--pack-id",
        help="Installed offline regional pack ID resolved under GRIDFORGE_PACKS_DIR (default /data/packs).",
    ),
    contour_density: DensityLevel = typer.Option(
        DensityLevel.NORMAL,
        "--contour-density",
        case_sensitive=False,
        help="Contour interval density (low, normal, high).",
    ),
    hillshade_strength: HillshadeStrength = typer.Option(
        HillshadeStrength.NORMAL,
        "--hillshade-strength",
        case_sensitive=False,
        help="Multi-directional hillshade intensity (off, light, normal, strong).",
    ),
    label_density: DensityLevel = typer.Option(
        DensityLevel.NORMAL,
        "--label-density",
        case_sensitive=False,
        help="Cartographic label density (low, normal, high).",
    ),
    buildings: bool = typer.Option(True, "--buildings/--no-buildings", help="Show building footprints & usage icons."),
    trees: bool = typer.Option(True, "--trees/--no-trees", help="Show evergreen/deciduous/mixed tree cover."),
    power: bool = typer.Option(True, "--power/--no-power", help="Show power lines, towers, and substations."),
    pipelines: bool = typer.Option(True, "--pipelines/--no-pipelines", help="Show pipelines."),
    grid: bool = typer.Option(True, "--grid/--no-grid", help="Include MGRS 1 km grid on exported map."),
    river_arrows: bool = typer.Option(
        True,
        "--river-arrows/--no-river-arrows",
        help="Show directional downstream flow arrows on rivers & streams.",
    ),
    slope_shading: bool = typer.Option(
        False,
        "--slope-shading/--no-slope-shading",
        help="Overlay backcountry slope-angle avalanche shading (>27 deg Color / >30 deg B&W).",
    ),
    waypoint: Optional[list[str]] = typer.Option(
        None,
        "--waypoint",
        help='Add a numbered tactical waypoint as "Label,lat,lon[,category]" (waypoint|camp|water|hazard|lz); repeatable.',
    ),
    viewshed: Optional[list[str]] = typer.Option(
        None,
        "--viewshed",
        help='Add an RF/LOS viewshed observer as "lat,lon[,obs_m[,tgt_m[,max_km[,label]]]]"; repeatable.',
    ),
    rf_link: Optional[list[str]] = typer.Option(
        None,
        "--rf-link",
        help='Add a point-to-point RF Fresnel link as "from_lat,from_lon,to_lat,to_lon[,freq_mhz[,from_h[,to_h[,from_lbl[,to_lbl]]]]]"; repeatable.',
    ),
    range_ring: Optional[list[str]] = typer.Option(
        None,
        "--range-ring",
        help='Add concentric tactical range rings as "lat,lon[,r1/r2/r3[,label]]" (km); repeatable.',
    ),
    tactical_line: Optional[list[str]] = typer.Option(
        None,
        "--tactical-line",
        help='Add a tactical bearing line as "lat1,lon1,lat2,lon2[,label]"; repeatable.',
    ),
    offline_dem: Optional[Path] = typer.Option(None, "--offline-dem", help="Local GeoTIFF/VRT or directory of DEMs."),
    offline_pbf: Optional[Path] = typer.Option(None, "--offline-pbf", help="Local .osm.pbf or .osm XML file."),
    overlay: Optional[list[Path]] = typer.Option(None, "--overlay", help="Optional user overlay (.geojson, .gpx, .csv)."),
    date_iso: Optional[str] = typer.Option(None, "--date", help="ISO date (YYYY-MM-DD) for metadata."),
    keep_workspace: bool = typer.Option(False, "--keep-workspace/--no-keep-workspace", help="Retain temp workspace."),
    work_dir: Optional[Path] = typer.Option(None, "--work-dir", help="Explicit workspace directory."),
    json_output: bool = typer.Option(False, "--json/--no-json", help="Print ExportResult list as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Export an offline ATAK ``.mbtiles``, Garmin ``.kmz``, GeoPackage ``.gpkg``, OsmAnd ``.sqlitedb``, or Field Bundle ``.zip``."""
    _configure_logging(verbose)

    carto = CartoOptions(
        contour_density=contour_density,
        hillshade_strength=hillshade_strength,
        label_density=label_density,
        show_buildings=buildings,
        show_trees=trees,
        show_power=power,
        show_pipelines=pipelines,
        show_grid=grid,
        show_river_arrows=river_arrows,
        show_slope_shading=slope_shading,
    )
    spec = _build_spec(
        center=center,
        scale=scale,
        paper=paper,
        orientation=orientation,
        mode=mode,
        units=units,
        title=title,
        date_iso=date_iso,
        dpi=dpi,
        dem_source=dem_source,
        us_overlays=us_overlay,
        au_overlays=au_overlay,
        nz_overlays=nz_overlay,
        ca_overlays=ca_overlay,
        waypoints=_parse_waypoints(waypoint),
        viewsheds=_parse_viewsheds(viewshed),
        rf_links=_parse_rf_links(rf_link),
        tactical_graphics=[*_parse_range_rings(range_ring), *_parse_tactical_lines(tactical_line)],
        carto=carto,
    )

    elev_src: ElevationSource | None = LocalDEMSource(offline_dem) if offline_dem is not None else None
    vec_src: VectorSource | None = LocalPBFSource(offline_pbf) if offline_pbf is not None else None
    overlay_srcs: list[OverlaySource] = [UserOverlaySource(p) for p in (overlay or [])]
    pack_dir = _cli_pack(pack, pack_id)

    results = run_export_job(
        spec,
        export_format,
        output,
        elevation_source=elev_src,
        vector_source=vec_src,
        overlay_sources=overlay_srcs or None,
        pack_dir=pack_dir,
        mbtiles_format=mbtiles_format,
        garmin_profile=garmin_profile,
        max_tiles=max_tiles,
        include_grid=grid,
        dpi=dpi,
        keep_workspace=keep_workspace,
        work_dir=work_dir,
    )

    if json_output:
        typer.echo(json.dumps([json.loads(r.model_dump_json()) for r in results], indent=2))
    else:
        for res in results:
            zoom_info = (
                f", zooms z{res.min_zoom}..z{res.max_zoom}"
                if res.min_zoom is not None and res.max_zoom is not None
                else ""
            )
            px_info = (
                f", max_tile={res.max_tile_px[0]}x{res.max_tile_px[1]}px"
                if res.max_tile_px is not None
                else ""
            )
            typer.echo(
                f"Exported {res.format.value.upper()} {res.output_path} "
                f"({res.file_size_bytes:,} bytes, {res.tile_count} tiles{zoom_info}{px_info}) "
                f"in {res.elapsed_seconds:.2f}s"
            )


@app.command("inspect")
def inspect_cmd(
    center: str = typer.Option(
        ...,
        "--center",
        "-c",
        help='Center coordinate as "lat,lon" (e.g. "-33.732,150.312") or MGRS string.',
    ),
    scale: int = typer.Option(
        25000,
        "--scale",
        "-s",
        help="Map scale denominator (15000, 24000, 25000, 48000, 50000).",
    ),
    paper: PaperSize = typer.Option(
        PaperSize.A4,
        "--paper",
        "-p",
        case_sensitive=False,
        help="Physical paper size (letter, a4, legal, a3).",
    ),
    orientation: Orientation = typer.Option(
        Orientation.PORTRAIT,
        "--orientation",
        "-o",
        case_sensitive=False,
        help="Page orientation (portrait, landscape).",
    ),
    units: Units = typer.Option(
        Units.METRIC,
        "--units",
        "-u",
        case_sensitive=False,
        help="Units (metric, imperial).",
    ),
    date_iso: Optional[str] = typer.Option(
        None,
        "--date",
        help="ISO date (YYYY-MM-DD) for WMM2025 declination.",
    ),
    elevation: float = typer.Option(
        0.0,
        "--elevation",
        help="Elevation in meters for WMM2025 calculation.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json/--table",
        help="Output machine-readable JSON instead of formatted summary table.",
    ),
) -> None:
    """Inspect UTM projection, physical sheet footprint, and WMM2025 declination."""
    spec = _build_spec(
        center=center,
        scale=scale,
        paper=paper,
        orientation=orientation,
        mode=ColorMode.COLOR,
        units=units,
        date_iso=date_iso,
    )
    layout = compute_sheet_layout(spec)
    decl = compute_declination(layout, elevation_m=elevation)
    grid_spacing_mm = 1000.0 / spec.scale * 1000.0

    summary = {
        "center_lat": round(spec.center_lat, 6),
        "center_lon": round(spec.center_lon, 6),
        "center_mgrs": spec.center_mgrs,
        "utm_zone": f"{layout.utm_zone}{layout.utm_hemisphere}",
        "utm_epsg": f"EPSG:{layout.utm_epsg}",
        "gzd": layout.gzd,
        "center_easting_m": round(layout.center_easting, 2),
        "center_northing_m": round(layout.center_northing, 2),
        "scale": f"1:{spec.scale:,}",
        "paper": f"{spec.paper.value} ({spec.orientation.value})",
        "page_mm": {"width": layout.page_width_mm, "height": layout.page_height_mm},
        "frame_mm": {"width": round(layout.frame_width_mm, 2), "height": round(layout.frame_height_mm, 2)},
        "footprint_m": {"width": round(layout.utm_bbox.width, 2), "height": round(layout.utm_bbox.height, 2)},
        "mgrs_1km_spacing_mm": round(grid_spacing_mm, 4),
        "utm_bbox": {
            "min_x": round(layout.utm_bbox.min_x, 2),
            "min_y": round(layout.utm_bbox.min_y, 2),
            "max_x": round(layout.utm_bbox.max_x, 2),
            "max_y": round(layout.utm_bbox.max_y, 2),
        },
        "wgs84_bbox": {
            "west": round(layout.wgs84_bbox.min_x, 6),
            "south": round(layout.wgs84_bbox.min_y, 6),
            "east": round(layout.wgs84_bbox.max_x, 6),
            "north": round(layout.wgs84_bbox.max_y, 6),
        },
        "declination": {
            "model": decl.model_name,
            "decimal_year": round(decl.decimal_year, 3),
            "magnetic_declination_deg": round(decl.magnetic_declination_deg, 3),
            "annual_change_deg_per_yr": round(decl.annual_change_deg, 4),
            "grid_convergence_deg": round(decl.grid_convergence_deg, 3),
            "grid_convergence_mils": round(decl.grid_convergence_mils, 1),
            "gm_angle_deg": round(decl.gm_angle_deg, 3),
            "gm_angle_mils": round(decl.gm_angle_mils, 1),
        },
    }

    if json_output:
        typer.echo(json.dumps(summary, indent=2))
        return

    def _ew(val: float) -> str:
        return f"{abs(val):.2f}° {'E' if val >= 0 else 'W'}"

    rows = [
        ("Center (WGS84)", f"{spec.center_lat:.6f}, {spec.center_lon:.6f}"),
        ("UTM Zone / EPSG", f"{layout.utm_zone}{layout.utm_hemisphere} / EPSG:{layout.utm_epsg} (GZD {layout.gzd})"),
        ("Center UTM (m)", f"E {layout.center_easting:,.1f}  N {layout.center_northing:,.1f}"),
        ("Sheet & Scale", f"{spec.paper.value.upper()} {spec.orientation.value} @ 1:{spec.scale:,}"),
        ("Page Size (mm)", f"{layout.page_width_mm:.1f} x {layout.page_height_mm:.1f} mm"),
        ("Map Frame (mm)", f"{layout.frame_width_mm:.1f} x {layout.frame_height_mm:.1f} mm"),
        ("Ground Footprint", f"{layout.utm_bbox.width:,.1f} m x {layout.utm_bbox.height:,.1f} m"),
        ("1 km Grid Spacing", f"{grid_spacing_mm:.3f} mm"),
        (
            "WGS84 BBox",
            f"W {layout.wgs84_bbox.min_x:.5f}, S {layout.wgs84_bbox.min_y:.5f}, "
            f"E {layout.wgs84_bbox.max_x:.5f}, N {layout.wgs84_bbox.max_y:.5f}",
        ),
        (
            f"Mag Declination ({decl.model_name})",
            f"{_ew(decl.magnetic_declination_deg)} ({decl.annual_change_deg:+.3f}°/yr @ {decl.decimal_year:.2f})",
        ),
        (
            "Grid Convergence",
            f"{_ew(decl.grid_convergence_deg)} ({abs(decl.grid_convergence_mils):.1f} mils)",
        ),
        (
            "G-M Angle",
            f"{_ew(decl.gm_angle_deg)} ({abs(decl.gm_angle_mils):.1f} mils)",
        ),
    ]
    width = max(len(k) for k, _ in rows)
    for key, val in rows:
        typer.echo(f"{key:<{width}} : {val}")


@pack_app.command("list-regions")
def pack_list_regions_cmd(
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Optional filter: 'state' or 'tactical'.",
    ),
    group: Optional[str] = typer.Option(
        None,
        "--group",
        help="Optional region group filter: 'au_tactical', 'au_state', 'nz', or 'na'.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json/--table",
        help="Output machine-readable JSON instead of a formatted table.",
    ),
) -> None:
    """List curated global region presets (AU, NZ, North America) for offline packs."""
    from gridforge.packs.regions import REGION_GROUPS, list_regions

    if group is not None:
        grp_norm = group.strip().lower()
        if grp_norm not in REGION_GROUPS:
            raise typer.BadParameter(f"Invalid --group. Choose one of: {', '.join(REGION_GROUPS)}.")
        regions = list_regions(group=grp_norm)
    else:
        regions = list_regions()
    if category is not None:
        cat_norm = category.strip().lower()
        if cat_norm not in ("state", "tactical"):
            raise typer.BadParameter("Invalid --category. Choose 'state' or 'tactical'.")
        regions = [r for r in regions if r.category == cat_norm]

    if json_output:
        typer.echo(json.dumps([json.loads(r.model_dump_json()) for r in regions], indent=2))
        return

    header = ("REGION ID", "GROUP", "NAME", "UTM ZONES", "EST. SIZE")
    table_rows = [
        (
            r.region_id,
            r.group,
            r.name,
            ",".join(r.utm_zones),
            r.estimated_size_hint or "-",
        )
        for r in regions
    ]
    widths = [
        max(len(header[i]), *(len(row[i]) for row in table_rows)) if table_rows else len(header[i])
        for i in range(len(header))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    typer.echo(fmt.format(*header))
    typer.echo("  ".join("-" * w for w in widths))
    for row in table_rows:
        typer.echo(fmt.format(*row))


@pack_app.command("build")
def pack_build_cmd(
    region: str = typer.Option(
        ...,
        "--region",
        "-r",
        help='Catalog region ID (e.g. "tas", "blue-mountains", "nz-tongariro", "us-rockies", "ca-banff") or custom "w,s,e,n" WGS84 bbox.',
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-O",
        help="Destination directory for the offline regional pack (writes pack.json + data files).",
    ),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        "-n",
        help="Optional display name override stored in pack.json.",
    ),
    source_pbf: Optional[Path] = typer.Option(
        None,
        "--source-pbf",
        help="Optional local .osm.pbf/.osm file to extract from instead of downloading Geofabrik.",
    ),
    source_dem: Optional[Path] = typer.Option(
        None,
        "--source-dem",
        help="Optional local GeoTIFF/VRT DEM to clip from instead of streaming cloud elevation.",
    ),
    dem_tier: PackDemTier = typer.Option(
        PackDemTier.AUTO,
        "--dem-tier",
        case_sensitive=False,
        help="Elevation source tier (auto, srtm-1s, glo30, au-elvis, nz-linz, usgs3dep, ca-hrdem).",
    ),
    include_osm: bool = typer.Option(True, "--osm/--no-osm", help="Include clipped OSM PBF in pack."),
    include_dem: bool = typer.Option(True, "--dem/--no-dem", help="Include compressed DEM GeoTIFF in pack."),
    include_overlays: bool = typer.Option(
        True,
        "--overlays/--no-overlays",
        help="Include regional open-data overlays (AU/NZ/US/CA).",
    ),
    include_basemap: bool = typer.Option(
        True,
        "--basemap/--no-basemap",
        help="Generate an air-gapped terrain basemap.mbtiles (zoom 7–12) inside the pack.",
    ),
    overlay_tile_deg: float = typer.Option(
        1.0,
        "--overlay-tile-deg",
        min=0.1,
        max=5.0,
        help="Maximum spatial tile size (degrees) for low-RAM overlay chunking.",
    ),
    cache_dir: Optional[Path] = typer.Option(
        None,
        "--cache-dir",
        help="Optional download cache directory (defaults to GRIDFORGE_CACHE_DIR).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json/--no-json",
        help="Print resulting PackManifest as JSON.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Build a self-contained, air-gapped offline regional data pack disk-to-disk."""
    from gridforge.packs.builder import build_offline_pack

    _configure_logging(verbose)

    try:
        manifest = build_offline_pack(
            region,
            output,
            name=name,
            include_osm=include_osm,
            include_dem=include_dem,
            include_overlays=include_overlays,
            include_basemap=include_basemap,
            source_pbf=source_pbf,
            source_dem=source_dem,
            dem_tier=cast(
                Literal["auto", "srtm-1s", "glo30", "au-elvis", "nz-linz", "usgs3dep", "ca-hrdem"],
                dem_tier.value,
            ),
            cache_dir=cache_dir,
            overlay_tile_deg=overlay_tile_deg,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    if json_output:
        typer.echo(manifest.model_dump_json(indent=2))
        return

    typer.echo(
        f"Built offline pack '{manifest.pack_id}' ({manifest.name}) at {output.resolve()} "
        f"[{_format_bytes(manifest.size_bytes)}, UTM {', '.join(manifest.utm_zones)}, "
        f"datum={manifest.vertical_datum}, overlays={len(manifest.overlays)}, "
        f"basemap={'yes' if manifest.basemap_path else 'no'}]"
    )


@app.command("basemap-pack")
def basemap_pack_cmd(
    region: str = typer.Option(
        ...,
        "--region",
        "-r",
        help='Catalog region ID (e.g. "blue-mountains", "nz-tongariro", "us-rockies", "ca-banff") or custom "w,s,e,n" WGS84 bbox.',
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-O",
        help="Destination air-gapped terrain basemap .mbtiles file path.",
    ),
    source_dem: Optional[Path] = typer.Option(
        None,
        "--source-dem",
        help="Optional local GeoTIFF/VRT DEM to render from without network access.",
    ),
    min_zoom: int = typer.Option(7, "--min-zoom", min=4, max=14, help="Minimum basemap Web Mercator zoom (default 7)."),
    max_zoom: int = typer.Option(12, "--max-zoom", min=4, max=15, help="Maximum basemap Web Mercator zoom (default 12)."),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Optional MBTiles metadata name."),
    tile_format: MBTilesFormat = typer.Option(
        MBTilesFormat.JPG,
        "--tile-format",
        case_sensitive=False,
        help="Basemap tile image format (jpg, png).",
    ),
    cache_dir: Optional[Path] = typer.Option(
        None,
        "--cache-dir",
        help="Optional download cache directory (defaults to GRIDFORGE_CACHE_DIR).",
    ),
    json_output: bool = typer.Option(False, "--json/--no-json", help="Print build metadata as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Build a standalone air-gapped hypsometric + hillshade terrain ``basemap.mbtiles`` for Studio."""
    from gridforge.packs.basemap_builder import build_basemap_for_region

    _configure_logging(verbose)
    if min_zoom > max_zoom:
        raise typer.BadParameter("--min-zoom must be less than or equal to --max-zoom.")

    try:
        out_path = build_basemap_for_region(
            region,
            output,
            source_dem=source_dem,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            name=name,
            tile_format=cast(Literal["png", "jpg"], tile_format.value),
            cache_dir=cache_dir,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    size_bytes = out_path.stat().st_size if out_path.is_file() else 0
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "output_mbtiles": str(out_path),
                    "region": region,
                    "min_zoom": min_zoom,
                    "max_zoom": max_zoom,
                    "tile_format": tile_format.value,
                    "size_bytes": size_bytes,
                },
                indent=2,
            )
        )
        return

    typer.echo(
        f"Built offline terrain basemap {out_path} "
        f"[{_format_bytes(size_bytes)}, z{min_zoom}..z{max_zoom}, {tile_format.value.upper()}]"
    )


@pack_app.command("inspect")
def pack_inspect_cmd(
    pack_dir: Path = typer.Argument(
        ...,
        help="Directory containing pack.json.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json/--table",
        help="Output raw PackManifest JSON instead of a formatted summary.",
    ),
) -> None:
    """Inspect metadata, bounds, UTM zones, and bundled layers of an offline regional pack."""
    from gridforge.packs.builder import load_pack_manifest

    try:
        manifest = load_pack_manifest(pack_dir)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if json_output:
        typer.echo(manifest.model_dump_json(indent=2))
        return

    b = manifest.bbox_wgs84
    overlays_str = (
        ", ".join(f"{k} ({v})" for k, v in sorted(manifest.overlays.items()))
        if manifest.overlays
        else "none"
    )
    rows = [
        ("Pack ID", manifest.pack_id),
        ("Name", manifest.name),
        ("Region", manifest.region),
        ("Created (UTC)", manifest.created_at),
        ("Total Size", f"{_format_bytes(manifest.size_bytes)} ({manifest.size_bytes:,} bytes)"),
        ("UTM Zones", ", ".join(manifest.utm_zones) or "-"),
        (
            "WGS84 BBox",
            f"W {b.min_x:.4f}, S {b.min_y:.4f}, E {b.max_x:.4f}, N {b.max_y:.4f}",
        ),
        (
            "DEM",
            f"{manifest.dem_path} (vertical datum: {manifest.vertical_datum})"
            if manifest.dem_path
            else "none",
        ),
        ("OSM PBF", manifest.osm_pbf_path or "none"),
        ("Offline Basemap", manifest.basemap_path or "none"),
        ("Overlays", overlays_str),
        ("Attributions", " | ".join(manifest.attributions) if manifest.attributions else "none"),
    ]
    width = max(len(k) for k, _ in rows)
    for key, val in rows:
        typer.echo(f"{key:<{width}} : {val}")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind interface for the GridForge self-hosted web server.",
    ),
    port: int = typer.Option(
        8080,
        "--port",
        min=1,
        max=65535,
        help="TCP port to listen on (default 8080).",
    ),
    reload: bool = typer.Option(
        False,
        "--reload/--no-reload",
        help="Enable Uvicorn auto-reload (development only).",
    ),
    log_level: str = typer.Option(
        "info",
        "--log-level",
        help="Uvicorn log level (critical, error, warning, info, debug).",
    ),
) -> None:
    """Launch the GridForge self-hosted FastAPI web interface via Uvicorn."""
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError as exc:
        typer.echo(
            "Error: 'uvicorn' is not installed in this environment. "
            "Install GridForge web dependencies or run inside the GridForge Docker image.",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    uvicorn.run(
        "gridforge.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level.lower(),
    )


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
