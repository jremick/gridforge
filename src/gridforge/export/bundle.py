"""Atomic Field Bundle (.zip) packager with valid GPX 1.1 waypoints/observers/tactical markup."""

from __future__ import annotations

import time
import zipfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from gridforge.config import (
    ExportFormat,
    ExportResult,
    JobWorkspace,
    WaypointCategory,
)
from gridforge.export._atomic import staged_archive

_ALREADY_COMPRESSED_SUFFIXES: frozenset[str] = frozenset(
    {".kmz", ".mbtiles", ".gpkg", ".sqlitedb", ".zip", ".jpg", ".jpeg", ".png", ".webp"}
)

_WAYPOINT_GPX_SYMBOLS: dict[WaypointCategory, str] = {
    WaypointCategory.WAYPOINT: "Flag, Blue",
    WaypointCategory.CAMP: "Campground",
    WaypointCategory.WATER: "Drinking Water",
    WaypointCategory.HAZARD: "Skull and Crossbones",
    WaypointCategory.LZ: "Heliport",
}


def _xml_text(tag: str, value: str, indent: str = "    ") -> str:
    return f"{indent}<{tag}>{escape(value)}</{tag}>"


def build_field_gpx(workspace: JobWorkspace) -> str:
    """Serialize sheet center, waypoints, RF observers, RF links, and tactical graphics as GPX 1.1."""
    layout = workspace.layout
    spec = layout.spec
    bbox = layout.wgs84_bbox
    title = spec.title or f"GridForge {layout.gzd} {workspace.mgrs_meta.center_mgrs_10m}"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="GridForge" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
        'http://www.topografix.com/GPX/1/1/gpx.xsd">',
        "  <metadata>",
        _xml_text("name", title),
        _xml_text(
            "desc",
            f"GridForge 1:{spec.scale:,} ({layout.gzd}, EPSG:{layout.utm_epsg}, "
            f"MGRS {workspace.mgrs_meta.center_mgrs_10m})",
        ),
        _xml_text("time", now_iso),
        f'    <bounds minlat="{bbox.min_y:.6f}" minlon="{bbox.min_x:.6f}" '
        f'maxlat="{bbox.max_y:.6f}" maxlon="{bbox.max_x:.6f}"/>',
        "  </metadata>",
    ]

    # 1. User tactical waypoints
    for wp in spec.waypoints:
        lines.append(f'  <wpt lat="{wp.lat:.6f}" lon="{wp.lon:.6f}">')
        lines.append(_xml_text("name", wp.label))
        if wp.notes:
            lines.append(_xml_text("desc", wp.notes))
        lines.append(_xml_text("sym", _WAYPOINT_GPX_SYMBOLS.get(wp.category, "Flag, Blue")))
        lines.append(_xml_text("type", wp.category.value))
        lines.append("  </wpt>")

    # 2. RF / LOS viewshed observers
    for idx, obs in enumerate(spec.viewsheds, start=1):
        label = obs.label or f"OBS{idx}"
        desc = (
            f"RF/LOS Viewshed Observer: antenna {obs.observer_height_m:g} m AGL, "
            f"target {obs.target_height_m:g} m AGL, range {obs.max_distance_m / 1000.0:.2f} km"
        )
        lines.append(f'  <wpt lat="{obs.lat:.6f}" lon="{obs.lon:.6f}">')
        lines.append(_xml_text("name", label))
        lines.append(_xml_text("desc", desc))
        lines.append(_xml_text("sym", "Radio Beacon"))
        lines.append(_xml_text("type", "viewshed-observer"))
        lines.append("  </wpt>")

    # 3. Point-to-point RF link endpoints as waypoints
    for idx, link in enumerate(spec.rf_links, start=1):
        for role, lat, lon, h_m, label in (
            ("from", link.from_lat, link.from_lon, link.from_height_m, link.from_label),
            ("to", link.to_lat, link.to_lon, link.to_height_m, link.to_label),
        ):
            lines.append(f'  <wpt lat="{lat:.6f}" lon="{lon:.6f}">')
            lines.append(_xml_text("name", label))
            lines.append(
                _xml_text(
                    "desc",
                    f"RF Link #{idx} ({role}): {link.freq_mhz:g} MHz, height {h_m:g} m AGL",
                )
            )
            lines.append(_xml_text("sym", "Radio Beacon"))
            lines.append(_xml_text("type", "rf-link-endpoint"))
            lines.append("  </wpt>")

    # 4. Tactical graphic anchor waypoints (range rings & single-point graphics)
    for idx, tg in enumerate(spec.tactical_graphics, start=1):
        if tg.kind == "range_ring" and tg.points:
            lat, lon = tg.points[0]
            radii_str = ", ".join(f"{r:g} km" for r in tg.radii_km)
            desc = f"Range rings: {radii_str}"
            if tg.notes:
                desc = f"{desc} — {tg.notes}"
            lines.append(f'  <wpt lat="{lat:.6f}" lon="{lon:.6f}">')
            lines.append(_xml_text("name", tg.label or f"Range Ring {idx}"))
            lines.append(_xml_text("desc", desc))
            lines.append(_xml_text("sym", "Navaid, Red"))
            lines.append(_xml_text("type", "tactical-range-ring"))
            lines.append("  </wpt>")

    # Always include sheet center reference waypoint when no user points were defined
    if not (spec.waypoints or spec.viewsheds or spec.rf_links or any(
        tg.kind == "range_ring" and tg.points for tg in spec.tactical_graphics
    )):
        lines.append(f'  <wpt lat="{spec.center_lat:.6f}" lon="{spec.center_lon:.6f}">')
        lines.append(_xml_text("name", f"Sheet Center {workspace.mgrs_meta.center_mgrs_10m}"))
        lines.append(_xml_text("desc", f"GridForge 1:{spec.scale:,} sheet center"))
        lines.append(_xml_text("sym", "Waypoint"))
        lines.append(_xml_text("type", "sheet-center"))
        lines.append("  </wpt>")

    # 5. Routes for RF links and multi-point tactical graphics (bearing lines, corridors, sectors)
    for idx, link in enumerate(spec.rf_links, start=1):
        lines.append("  <rte>")
        lines.append(_xml_text("name", f"RF Link {idx}: {link.from_label} -> {link.to_label}"))
        lines.append(
            _xml_text(
                "desc",
                f"{link.freq_mhz:g} MHz (k={link.k_factor:.3f}), "
                f"{link.from_height_m:g} m -> {link.to_height_m:g} m AGL",
            )
        )
        lines.append(_xml_text("type", "rf-link"))
        lines.append(f'    <rtept lat="{link.from_lat:.6f}" lon="{link.from_lon:.6f}">')
        lines.append(_xml_text("name", link.from_label, indent="      "))
        lines.append("    </rtept>")
        lines.append(f'    <rtept lat="{link.to_lat:.6f}" lon="{link.to_lon:.6f}">')
        lines.append(_xml_text("name", link.to_label, indent="      "))
        lines.append("    </rtept>")
        lines.append("  </rte>")

    for idx, tg in enumerate(spec.tactical_graphics, start=1):
        if tg.kind in ("bearing_line", "corridor", "sector") and len(tg.points) >= 2:
            pts = list(tg.points)
            if tg.kind == "sector" and pts[0] != pts[-1]:
                pts.append(pts[0])
            lines.append("  <rte>")
            lines.append(_xml_text("name", tg.label or f"{tg.kind.replace('_', ' ').title()} {idx}"))
            if tg.notes:
                lines.append(_xml_text("desc", tg.notes))
            lines.append(_xml_text("type", f"tactical-{tg.kind}"))
            for pt_idx, (lat, lon) in enumerate(pts, start=1):
                lines.append(f'    <rtept lat="{lat:.6f}" lon="{lon:.6f}">')
                lines.append(_xml_text("name", f"P{pt_idx}", indent="      "))
                lines.append("    </rtept>")
            lines.append("  </rte>")

    lines.append("</gpx>")
    lines.append("")
    return "\n".join(lines)


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def build_field_bundle_readme(
    workspace: JobWorkspace,
    bundled_entries: Sequence[tuple[str, int]],
    *,
    extra_notes: Sequence[str] = (),
) -> str:
    """Build a crisp plain-text ``README_FIELD_BUNDLE.txt`` for offline field deployment."""
    layout = workspace.layout
    spec = layout.spec
    decl = workspace.declination
    title = spec.title or f"GridForge {layout.gzd} {workspace.mgrs_meta.center_mgrs_10m}"

    lines = [
        "========================================================================",
        "GRIDFORGE FIELD BUNDLE — AIR-GAPPED PRINT & DIGITAL MAP PACKAGE",
        "========================================================================",
        "",
        f"Sheet Title      : {title}",
        f"Scale / Paper    : 1:{spec.scale:,} · {spec.paper.value.upper()} {spec.orientation.value}",
        f"Theme / Units    : {spec.color_mode.value} · {spec.units.value}",
        f"Center (WGS84)   : {spec.center_lat:.6f}, {spec.center_lon:.6f}",
        f"Center (MGRS)    : {workspace.mgrs_meta.center_mgrs_10m} ({layout.gzd})",
        f"Projection       : EPSG:{layout.utm_epsg} (UTM Zone {layout.utm_zone}{layout.utm_hemisphere})",
        f"Vertical Datum   : {workspace.vertical_datum}",
        f"G-M Angle        : {decl.gm_angle_deg:+.2f} deg ({decl.gm_angle_mils:+.0f} mils, {decl.model_name})",
        "",
        "INCLUDED ARTIFACTS",
        "------------------",
    ]
    for arcname, size_bytes in bundled_entries:
        lines.append(f"  - {arcname:<44} ({_format_size(size_bytes)})")

    lines.extend(
        [
            "",
            "DEVICE IMPORT QUICKSTART",
            "------------------------",
            "  * Print / Avenza Maps (.pdf)  : Print at 100% / Actual Size (no Fit-to-Page).",
            "                                  If GeoPDF enabled, open directly in Avenza Maps.",
            "  * ATAK / WinTAK (.mbtiles)    : Copy to /sdcard/atak/imagery/ and open Imagery.",
            "  * ATAK / QGIS (.gpkg)         : Copy to /sdcard/atak/imagery/ or open in QGIS.",
            "  * Garmin Handheld (.kmz)      : Copy to Garmin/CustomMaps/ on device/SD card.",
            "  * OsmAnd (.sqlitedb)          : Open file with OsmAnd (BigPlanet offline tiles).",
            "  * Waypoints / Markup (.gpx)   : Import waypoints.gpx into ATAK, Garmin, or OsmAnd.",
        ]
    )

    if extra_notes:
        lines.extend(["", "NOTES", "-----"])
        for note in extra_notes:
            lines.append(f"  - {note}")

    attributions = workspace.attributions or ["Map data © OpenStreetMap contributors (ODbL)"]
    lines.extend(["", "DATA ATTRIBUTIONS", "-----------------"])
    for attr in attributions:
        lines.append(f"  - {attr}")
    lines.append("")
    return "\n".join(lines)


def export_field_bundle_zip(
    workspace: JobWorkspace,
    output_zip: Path,
    *,
    artifacts: Sequence[Path | tuple[str, Path]] = (),
    gpx_filename: str = "waypoints.gpx",
    readme_filename: str = "README_FIELD_BUNDLE.txt",
    extra_notes: Sequence[str] = (),
) -> ExportResult:
    """Atomically package all enabled job artifacts + GPX 1.1 + field README into a ``.zip``."""
    t0 = time.perf_counter()

    caller_out = Path(output_zip)
    resolved_out = caller_out.resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    normalized_artifacts: list[tuple[str, Path]] = []
    seen_names: set[str] = set()
    for item in artifacts:
        if isinstance(item, tuple):
            arcname, path_obj = item[0], Path(item[1]).resolve()
        else:
            path_obj = Path(item).resolve()
            arcname = path_obj.name
        if not path_obj.is_file():
            raise FileNotFoundError(f"Field bundle artifact not found: '{path_obj}'")
        safe_name = Path(arcname).name
        if not safe_name or safe_name in (gpx_filename, readme_filename):
            raise ValueError(f"Invalid or reserved archive entry name '{arcname}'.")
        if safe_name in seen_names:
            continue
        seen_names.add(safe_name)
        normalized_artifacts.append((safe_name, path_obj))

    gpx_bytes = build_field_gpx(workspace).encode("utf-8")

    with staged_archive(resolved_out) as staged:
        bundled_entries: list[tuple[str, int]] = []
        with zipfile.ZipFile(staged, mode="w") as zf:
            for arcname, path_obj in normalized_artifacts:
                compress = (
                    zipfile.ZIP_STORED
                    if path_obj.suffix.lower() in _ALREADY_COMPRESSED_SUFFIXES
                    else zipfile.ZIP_DEFLATED
                )
                zf.write(path_obj, arcname=arcname, compress_type=compress)
                bundled_entries.append((arcname, path_obj.stat().st_size))

            zf.writestr(gpx_filename, gpx_bytes, compress_type=zipfile.ZIP_DEFLATED)
            bundled_entries.append((gpx_filename, len(gpx_bytes)))

            readme_bytes = build_field_bundle_readme(
                workspace,
                [*bundled_entries, (readme_filename, 0)],
                extra_notes=extra_notes,
            ).encode("utf-8")
            # Re-render README once with accurate final byte size for the README entry itself.
            readme_bytes = build_field_bundle_readme(
                workspace,
                [*bundled_entries, (readme_filename, len(readme_bytes))],
                extra_notes=extra_notes,
            ).encode("utf-8")
            zf.writestr(readme_filename, readme_bytes, compress_type=zipfile.ZIP_DEFLATED)
            bundled_entries.append((readme_filename, len(readme_bytes)))

        elapsed = round(time.perf_counter() - t0, 3)
        return ExportResult(
            format=ExportFormat.BUNDLE,
            output_path=caller_out,
            file_size_bytes=staged.stat().st_size,
            tile_count=len(bundled_entries),
            min_zoom=None,
            max_zoom=None,
            max_tile_px=None,
            max_tile_bytes=None,
            elapsed_seconds=elapsed,
        )
