"""GridForge Typer CLI (``gridforge render`` and ``gridforge inspect``)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import typer

from gridforge.config import (
    VALID_SCALES,
    ColorMode,
    Orientation,
    PaperSize,
    SheetSpec,
    Units,
)
from gridforge.geo.crs import compute_sheet_layout, parse_center
from gridforge.geo.declination import compute_declination
from gridforge.pipeline.runner import run_render_job
from gridforge.sources.base import ElevationSource, OverlaySource, VectorSource
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource
from gridforge.sources.overlay_user import UserOverlaySource

app = typer.Typer(
    name="gridforge",
    help="GridForge — Military-style true-scale MGRS topographic map generator.",
    no_args_is_help=True,
)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


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
    )


@app.command("render")
def render_cmd(
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
    mode: ColorMode = typer.Option(
        ColorMode.COLOR,
        "--mode",
        "-m",
        case_sensitive=False,
        help="Cartographic theme (color, bw).",
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
    """Render a print-ready, physically true-scale MGRS topographic PDF."""
    _configure_logging(verbose)

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
    )

    elev_src: ElevationSource | None = LocalDEMSource(offline_dem) if offline_dem is not None else None
    vec_src: VectorSource | None = LocalPBFSource(offline_pbf) if offline_pbf is not None else None
    overlay_srcs: list[OverlaySource] = [UserOverlaySource(p) for p in (overlay or [])]

    result = run_render_job(
        spec,
        output,
        elevation_source=elev_src,
        vector_source=vec_src,
        overlay_sources=overlay_srcs or None,
        keep_workspace=keep_workspace,
        work_dir=work_dir,
    )

    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(
            f"Rendered {result.output_pdf} "
            f"({result.page_width_mm:.1f}x{result.page_height_mm:.1f} mm, "
            f"1:{result.scale:,}, EPSG:{result.utm_epsg}) in {result.elapsed_seconds:.2f}s"
        )
        if result.workspace_dir is not None:
            typer.echo(f"Workspace retained at: {result.workspace_dir}")


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


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
