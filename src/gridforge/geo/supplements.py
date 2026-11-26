"""True-scale supplemental vector PDF pages via isolated PyQt5 QPdfWriter subprocesses.

Generates:
  - 100.00 mm Printer Scale Verification & MGRS Romer / 6400-mil Protractor page
  - Tactical Route Card & Waypoint Log page
  - Route Overview & DEM Elevation Profile page
  - Atlas Overview & Sheet Index Cover Page (Grid Atlases & GPX Strip Atlases)
  - Lossless multi-page PDF merge via ``pypdf.PdfWriter``

Subprocess isolation ensures the FastAPI web backend never imports PyQt5 or Qt.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pypdf import PdfWriter

from gridforge.config import BBox, DeclinationInfo, SheetLayout, Units
from gridforge.geo.strip_atlas import (
    KM_TO_MILES,
    METERS_TO_FEET,
    ElevationProfileData,
    StripAtlasPlan,
    StripSheetPlan,
)
from gridforge.geo.waypoints import RouteMetrics

PDF_DPI: int = 300
MM_TO_PX: float = PDF_DPI / 25.4


class AtlasIndexSheet(BaseModel):
    """Single sheet footprint entry rendered on the Atlas Overview & Sheet Index Cover Page."""

    sheet_id: str
    page_number: int = 1
    center_lat: float
    center_lon: float
    center_mgrs_10m: str
    utm_bbox: BBox


def merge_pdf_pages(pdf_paths: list[Path], output_pdf: Path) -> Path:
    """Losslessly merge ordered PDF pages into ``output_pdf`` via ``pypdf.PdfWriter``."""
    valid = [Path(p) for p in pdf_paths if Path(p).is_file()]
    if not valid:
        raise ValueError("No existing PDF files provided to merge_pdf_pages().")

    resolved_out = Path(output_pdf).resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = resolved_out.with_suffix(".merge_tmp.pdf")

    writer = PdfWriter()
    try:
        for p in valid:
            writer.append(str(p))
        with tmp_out.open("wb") as fh:
            writer.write(fh)
    finally:
        writer.close()

    tmp_out.replace(resolved_out)
    return Path(output_pdf)


def _spawn_supplement_worker(mode: str, payload: dict[str, Any], output_pdf: Path) -> Path:
    """Run QPdfWriter rendering inside an ephemeral offscreen Python subprocess."""
    caller_out = Path(output_pdf)
    resolved_out = caller_out.resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gf_supp_") as tmp_dir_s:
        spec_path = Path(tmp_dir_s) / "supplement_spec.json"
        spec_path.write_text(json.dumps(payload), encoding="utf-8")

        env = os.environ.copy()
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        runtime_dir = Path(env.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root"))
        runtime_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            "-m",
            "gridforge.geo.supplements",
            mode,
            str(spec_path),
            str(resolved_out),
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Supplemental PDF render ({mode}) failed (exit={proc.returncode}).\n"
                f"STDERR:\n{proc.stderr[-2500:]}\nSTDOUT:\n{proc.stdout[-1500:]}"
            )

    if not resolved_out.is_file():
        raise RuntimeError(f"Supplemental PDF render ({mode}) did not create '{resolved_out}'.")
    return caller_out


def render_calibration_pdf(
    page_width_mm: float,
    page_height_mm: float,
    scale: int,
    output_pdf: Path,
    *,
    bw: bool = False,
) -> Path:
    """Render an exact 100.00 mm / 4.00 in Scale Calibration & MGRS Romer PDF page."""
    payload = {
        "page_width_mm": float(page_width_mm),
        "page_height_mm": float(page_height_mm),
        "scale": int(scale),
        "bw": bool(bw),
    }
    return _spawn_supplement_worker("calibration", payload, output_pdf)


def render_route_card_pdf(
    metrics: RouteMetrics,
    layout: SheetLayout,
    declination: DeclinationInfo,
    output_pdf: Path,
    *,
    bw: bool = False,
) -> Path:
    """Render a military Tactical Route Card & Waypoint Log PDF page."""
    payload = {
        "metrics": json.loads(metrics.model_dump_json()),
        "layout": json.loads(layout.model_dump_json()),
        "declination": json.loads(declination.model_dump_json()),
        "bw": bool(bw),
    }
    return _spawn_supplement_worker("route-card", payload, output_pdf)


def render_elevation_profile_pdf(
    profile: ElevationProfileData,
    strip_plan: StripAtlasPlan | None,
    layout: SheetLayout,
    output_pdf: Path,
    *,
    bw: bool = False,
) -> Path:
    """Render a Route Overview & Elevation Profile PDF page (with optional strip index table)."""
    payload = {
        "profile": json.loads(profile.model_dump_json()),
        "strip_plan": json.loads(strip_plan.model_dump_json()) if strip_plan is not None else None,
        "layout": json.loads(layout.model_dump_json()),
        "bw": bool(bw),
    }
    return _spawn_supplement_worker("elevation-profile", payload, output_pdf)


def render_atlas_index_pdf(
    sheets: Sequence[AtlasIndexSheet | StripSheetPlan],
    *,
    page_width_mm: float,
    page_height_mm: float,
    scale: int,
    utm_epsg: int,
    title: str,
    overlap_pct: float = 0.0,
    route_utm: list[tuple[float, float]] | None = None,
    output_pdf: Path,
    bw: bool = False,
) -> Path:
    """Render an exact-mm pure-vector Atlas Overview & Sheet Index Cover Page.

    Works seamlessly for both Grid Atlases (``A1..C3``) and GPX/GeoJSON Strip Atlases
    (``S1..Sn``). Accepts either ``AtlasIndexSheet`` or ``StripSheetPlan`` entries.
    """
    if not sheets:
        raise ValueError("render_atlas_index_pdf() requires at least one sheet.")

    normalized: list[AtlasIndexSheet] = []
    for idx, sh in enumerate(sheets, start=1):
        if isinstance(sh, AtlasIndexSheet):
            normalized.append(sh)
        else:
            normalized.append(
                AtlasIndexSheet(
                    sheet_id=sh.sheet_id,
                    page_number=int(getattr(sh, "index", idx)) + 1,
                    center_lat=float(sh.center_lat),
                    center_lon=float(sh.center_lon),
                    center_mgrs_10m=str(sh.center_mgrs_10m),
                    utm_bbox=sh.utm_bbox,
                )
            )

    payload = {
        "sheets": [json.loads(s.model_dump_json()) for s in normalized],
        "page_width_mm": float(page_width_mm),
        "page_height_mm": float(page_height_mm),
        "scale": int(scale),
        "utm_epsg": int(utm_epsg),
        "title": str(title or "GRIDFORGE ATLAS"),
        "overlap_pct": float(overlap_pct),
        "route_utm": [[float(x), float(y)] for x, y in route_utm] if route_utm else None,
        "bw": bool(bw),
    }
    return _spawn_supplement_worker("atlas-index", payload, output_pdf)


def render_rf_link_card_pdf(
    profiles: Sequence[dict[str, Any] | BaseModel],
    layout: SheetLayout,
    output_pdf: Path,
    *,
    bw: bool = False,
) -> Path:
    """Render an exact-mm pure-vector RF / Line-of-Sight Link Budget & Fresnel Profile Card."""
    if not profiles:
        raise ValueError("render_rf_link_card_pdf() requires at least one RF link profile.")

    serialized: list[dict[str, Any]] = []
    for p in profiles:
        if isinstance(p, BaseModel):
            serialized.append(json.loads(p.model_dump_json()))
        elif isinstance(p, dict):
            serialized.append(dict(p))
        else:
            raise TypeError(f"Unsupported RF link profile entry: {type(p)!r}")

    payload = {
        "profiles": serialized,
        "layout": json.loads(layout.model_dump_json()),
        "bw": bool(bw),
    }
    return _spawn_supplement_worker("rf-link-card", payload, output_pdf)


# ---------------------------------------------------------------------------
# Subprocess-only PyQt5 QPdfWriter vector drawing implementations
# ---------------------------------------------------------------------------


def _px(mm: float) -> float:
    return mm * MM_TO_PX


def _setup_pdf_writer(output_pdf: Path, page_width_mm: float, page_height_mm: float) -> Any:
    from PyQt5.QtCore import QMarginsF, QSizeF
    from PyQt5.QtGui import QPageLayout, QPageSize, QPdfWriter

    writer = QPdfWriter(str(output_pdf))
    writer.setResolution(PDF_DPI)
    # Keep explicit (width, height) with Portrait orientation so landscape pages are not double-swapped!
    psize = QPageSize(QSizeF(page_width_mm, page_height_mm), QPageSize.Millimeter)
    playout = QPageLayout(
        psize,
        QPageLayout.Portrait,
        QMarginsF(0.0, 0.0, 0.0, 0.0),
        QPageLayout.Millimeter,
    )
    playout.setMode(QPageLayout.FullPageMode)
    writer.setPageLayout(playout)
    return writer


def _make_pen(color_hex: str, width_mm: float, *, dashed: bool = False) -> Any:
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor, QPen

    pen = QPen(QColor(color_hex))
    pen.setWidthF(_px(width_mm))
    pen.setCapStyle(Qt.FlatCap)
    pen.setJoinStyle(Qt.MiterJoin)
    if dashed:
        pen.setStyle(Qt.DashLine)
    return pen


def _make_font(pt: float, *, bold: bool = False, mono: bool = False) -> Any:
    from PyQt5.QtGui import QFont

    family = "DejaVu Sans Mono" if mono else "Liberation Sans"
    f = QFont(family)
    f.setPointSizeF(pt)
    f.setBold(bold)
    return f


def _draw_text_box(
    painter: Any,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    text: str,
    *,
    pt: float = 8.0,
    bold: bool = False,
    mono: bool = False,
    color_hex: str = "#111111",
    align: str = "left",
    wrap: bool = False,
) -> None:
    from PyQt5.QtCore import QRectF, Qt
    from PyQt5.QtGui import QColor

    painter.setPen(QColor(color_hex))
    painter.setFont(_make_font(pt, bold=bold, mono=mono))
    flags = Qt.AlignVCenter
    if align == "center":
        flags |= Qt.AlignHCenter
    elif align == "right":
        flags |= Qt.AlignRight
    else:
        flags |= Qt.AlignLeft
    if wrap:
        flags |= Qt.TextWordWrap
    painter.drawText(QRectF(_px(x_mm), _px(y_mm), _px(w_mm), _px(h_mm)), flags, text)


def _draw_romer_corner(
    painter: Any,
    origin_x_mm: float,
    origin_y_mm: float,
    km_mm: float,
    scale_label: str,
    *,
    is_active: bool,
    ink: str,
    accent: str,
    muted: str,
) -> None:
    """Draw a true-scale cut-out L-shaped MGRS Romer corner (0..10 hundred-meter ticks)."""
    from PyQt5.QtCore import QPointF

    step_mm = km_mm / 10.0
    stroke_col = accent if is_active else ink

    # Outer square guide (light dashed) so users can align cleanly with a 1 km grid square
    painter.setPen(_make_pen("#999999", 0.15, dashed=True))
    painter.drawLine(
        QPointF(_px(origin_x_mm), _px(origin_y_mm)),
        QPointF(_px(origin_x_mm + km_mm), _px(origin_y_mm)),
    )
    painter.drawLine(
        QPointF(_px(origin_x_mm), _px(origin_y_mm)),
        QPointF(_px(origin_x_mm), _px(origin_y_mm + km_mm)),
    )

    # Primary Romer measuring axes along bottom (Easting: read right -> left from corner)
    # and right (Northing: read up from corner) meeting at bottom-right origin (0, 0).
    corner_x = origin_x_mm + km_mm
    corner_y = origin_y_mm + km_mm

    painter.setPen(_make_pen(stroke_col, 0.35 if is_active else 0.25))
    painter.drawLine(QPointF(_px(origin_x_mm), _px(corner_y)), QPointF(_px(corner_x), _px(corner_y)))
    painter.drawLine(QPointF(_px(corner_x), _px(origin_y_mm)), QPointF(_px(corner_x), _px(corner_y)))

    # 10 subdivisions (0..10 hundred meters)
    for k in range(11):
        tick_len = 2.6 if k % 5 == 0 else 1.5
        # Horizontal Easting scale (0 at right corner, 10 at left edge)
        tx = corner_x - k * step_mm
        painter.setPen(_make_pen(stroke_col, 0.22 if k % 5 == 0 else 0.15))
        painter.drawLine(QPointF(_px(tx), _px(corner_y)), QPointF(_px(tx), _px(corner_y - tick_len)))
        if k in (0, 2, 4, 6, 8, 10) and (km_mm >= 30.0 or k in (0, 5, 10)):
            _draw_text_box(
                painter,
                tx - 2.5,
                corner_y + 0.6,
                5.0,
                3.2,
                str(k),
                pt=5.5,
                align="center",
                color_hex=ink,
            )

        # Vertical Northing scale (0 at bottom corner, 10 at top edge)
        ty = corner_y - k * step_mm
        painter.drawLine(QPointF(_px(corner_x), _px(ty)), QPointF(_px(corner_x - tick_len), _px(ty)))
        if k in (0, 2, 4, 6, 8, 10) and (km_mm >= 30.0 or k in (0, 5, 10)):
            _draw_text_box(
                painter,
                corner_x + 0.8,
                ty - 1.6,
                5.0,
                3.2,
                str(k),
                pt=5.5,
                align="left",
                color_hex=ink,
            )

    badge = f"{scale_label} ★" if is_active else scale_label
    _draw_text_box(
        painter,
        origin_x_mm + 1.0,
        origin_y_mm + 1.5,
        km_mm - 3.0,
        5.0,
        badge,
        pt=6.8 if km_mm >= 30.0 else 5.8,
        bold=True,
        color_hex=stroke_col,
    )
    _draw_text_box(
        painter,
        origin_x_mm + 1.0,
        origin_y_mm + 6.0,
        km_mm - 3.0,
        4.0,
        f"1 km = {km_mm:.2f} mm",
        pt=5.4,
        color_hex=muted,
    )


def _draw_protractor_ring(
    painter: Any,
    cx_mm: float,
    cy_mm: float,
    radius_mm: float,
    *,
    ink: str,
    muted: str,
) -> None:
    """Draw a crisp 360° / 6400 NATO-mil compass protractor reference ring."""
    from PyQt5.QtCore import QPointF, QRectF

    painter.setPen(_make_pen(ink, 0.30))
    painter.drawEllipse(
        QRectF(_px(cx_mm - radius_mm), _px(cy_mm - radius_mm), _px(2 * radius_mm), _px(2 * radius_mm))
    )
    inner_r = radius_mm - 7.5
    painter.setPen(_make_pen(muted, 0.18))
    painter.drawEllipse(
        QRectF(_px(cx_mm - inner_r), _px(cy_mm - inner_r), _px(2 * inner_r), _px(2 * inner_r))
    )

    # Center crosshair
    painter.setPen(_make_pen(ink, 0.20))
    painter.drawLine(QPointF(_px(cx_mm - 4.0), _px(cy_mm)), QPointF(_px(cx_mm + 4.0), _px(cy_mm)))
    painter.drawLine(QPointF(_px(cx_mm), _px(cy_mm - 4.0)), QPointF(_px(cx_mm), _px(cy_mm + 4.0)))

    # Outer degree ticks every 5° (labeled every 30°)
    for deg in range(0, 360, 5):
        rad = math.radians(deg)
        ux, uy = math.sin(rad), -math.cos(rad)
        tlen = 3.2 if deg % 30 == 0 else (2.0 if deg % 10 == 0 else 1.1)
        painter.setPen(_make_pen(ink, 0.24 if deg % 30 == 0 else 0.14))
        painter.drawLine(
            QPointF(_px(cx_mm + (radius_mm - tlen) * ux), _px(cy_mm + (radius_mm - tlen) * uy)),
            QPointF(_px(cx_mm + radius_mm * ux), _px(cy_mm + radius_mm * uy)),
        )
        if deg % 30 == 0:
            lr = radius_mm + 4.2
            lx = cx_mm + lr * ux
            ly = cy_mm + lr * uy
            lbl = {0: "0°/N", 90: "90°/E", 180: "180°/S", 270: "270°/W"}.get(deg, f"{deg}°")
            _draw_text_box(
                painter,
                lx - 7.0,
                ly - 2.2,
                14.0,
                4.4,
                lbl,
                pt=5.8,
                bold=(deg % 90 == 0),
                align="center",
                color_hex=ink,
            )

    # Inner NATO mils ticks every 200 mils (labeled every 800 mils)
    for mil in range(0, 6400, 200):
        rad = math.radians(mil * 360.0 / 6400.0)
        ux, uy = math.sin(rad), -math.cos(rad)
        tlen = 2.4 if mil % 800 == 0 else 1.3
        painter.setPen(_make_pen(muted, 0.20 if mil % 800 == 0 else 0.12))
        painter.drawLine(
            QPointF(_px(cx_mm + inner_r * ux), _px(cy_mm + inner_r * uy)),
            QPointF(_px(cx_mm + (inner_r - tlen) * ux), _px(cy_mm + (inner_r - tlen) * uy)),
        )
        if mil % 800 == 0:
            lr = inner_r - 5.6
            lx = cx_mm + lr * ux
            ly = cy_mm + lr * uy
            _draw_text_box(
                painter,
                lx - 5.5,
                ly - 2.0,
                11.0,
                4.0,
                f"{mil}",
                pt=5.2,
                align="center",
                color_hex=muted,
            )

    _draw_text_box(
        painter,
        cx_mm - 20.0,
        cy_mm + 5.5,
        40.0,
        4.5,
        "360° / 6400 NATO MILS",
        pt=5.6,
        bold=True,
        align="center",
        color_hex=muted,
    )


def _render_calibration_in_process(spec: dict[str, Any], output_pdf: Path) -> None:
    from PyQt5.QtCore import QPointF, QRectF
    from PyQt5.QtGui import QColor, QPainter

    page_w = float(spec["page_width_mm"])
    page_h = float(spec["page_height_mm"])
    scale = int(spec["scale"])
    bw = bool(spec.get("bw", False))

    ink = "#000000" if bw else "#111820"
    muted = "#333333" if bw else "#4B5563"
    accent = "#000000" if bw else "#1B4F72"

    writer = _setup_pdf_writer(output_pdf, page_w, page_h)
    painter = QPainter()
    if not painter.begin(writer):
        raise RuntimeError(f"Failed to start QPainter for '{output_pdf}'.")

    try:
        margin = 12.0
        inner_w = page_w - 2.0 * margin

        # Crisp page border
        painter.setPen(_make_pen(ink, 0.35))
        painter.drawRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(page_h - 2.0 * margin)))

        # Header banner
        painter.fillRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(12.0)), QColor(ink))
        _draw_text_box(
            painter,
            margin + 4.0,
            margin + 1.0,
            inner_w - 8.0,
            10.0,
            f"PRINTER SCALE VERIFICATION & MGRS ROMER SHEET  |  ACTIVE SCALE 1:{scale:,}",
            pt=9.5,
            bold=True,
            color_hex="#FFFFFF",
        )

        # Verification instructions
        _draw_text_box(
            painter,
            margin + 4.0,
            margin + 14.5,
            inner_w - 8.0,
            11.0,
            "PRINTER SCALE VERIFICATION — Measure with a physical ruler. Both black verification "
            "rulers must equal exactly 100.0 mm (Imperial bar = 4.00 in / 101.6 mm). "
            "If off, reprint at 100% / Actual Size (disable Fit to Page / Shrink to Printable Area).",
            pt=7.5,
            bold=True,
            color_hex=ink,
            wrap=True,
        )

        # --- 1. EXACT 100.00 mm HORIZONTAL RULER ---
        hr_x = margin + 8.0
        hr_y = margin + 39.0
        _draw_text_box(
            painter,
            hr_x,
            hr_y - 11.5,
            105.0,
            4.5,
            "HORIZONTAL VERIFICATION RULER — EXACTLY 100.00 mm",
            pt=7.2,
            bold=True,
            color_hex=ink,
        )
        # Continuous pure-vector 100.00 mm baseline segment!
        painter.setPen(_make_pen(ink, 0.40))
        painter.drawLine(QPointF(_px(hr_x), _px(hr_y)), QPointF(_px(hr_x + 100.0), _px(hr_y)))
        for mm in range(101):
            tlen = 4.5 if mm % 10 == 0 else (3.0 if mm % 5 == 0 else 1.8)
            painter.setPen(_make_pen(ink, 0.25 if mm % 10 == 0 else 0.14))
            painter.drawLine(
                QPointF(_px(hr_x + mm), _px(hr_y)),
                QPointF(_px(hr_x + mm), _px(hr_y - tlen)),
            )
            if mm % 10 == 0:
                _draw_text_box(
                    painter,
                    hr_x + mm - 4.0,
                    hr_y + 1.0,
                    8.0,
                    3.6,
                    f"{mm}",
                    pt=6.0,
                    bold=(mm in (0, 50, 100)),
                    align="center",
                    color_hex=ink,
                )
        _draw_text_box(
            painter, hr_x + 102.0, hr_y - 2.0, 16.0, 4.0, "100 mm", pt=6.5, bold=True, color_hex=ink
        )

        # --- 2. EXACT 4.00 in (101.60 mm) IMPERIAL VERIFICATION BAR ---
        ir_x = hr_x
        ir_y = hr_y + 18.0
        _draw_text_box(
            painter,
            ir_x,
            ir_y - 10.5,
            110.0,
            4.5,
            "IMPERIAL VERIFICATION BAR — EXACTLY 4.00 in (101.60 mm)",
            pt=7.0,
            bold=True,
            color_hex=muted,
        )
        painter.setPen(_make_pen(ink, 0.35))
        painter.drawLine(QPointF(_px(ir_x), _px(ir_y)), QPointF(_px(ir_x + 101.6), _px(ir_y)))
        for q in range(17):  # 1/4 inch increments up to 4.00 in
            x_pos = ir_x + q * (25.4 / 4.0)
            tlen = 4.2 if q % 4 == 0 else (2.8 if q % 2 == 0 else 1.6)
            painter.setPen(_make_pen(ink, 0.24 if q % 4 == 0 else 0.14))
            painter.drawLine(QPointF(_px(x_pos), _px(ir_y)), QPointF(_px(x_pos), _px(ir_y - tlen)))
            if q % 4 == 0:
                _draw_text_box(
                    painter,
                    x_pos - 4.0,
                    ir_y + 1.0,
                    8.0,
                    3.6,
                    f'{q // 4}"',
                    pt=6.0,
                    bold=True,
                    align="center",
                    color_hex=ink,
                )

        # --- 3. EXACT 100.00 mm VERTICAL RULER ---
        vr_x = min(page_w - margin - 26.0, hr_x + 136.0)
        vr_y = hr_y
        _draw_text_box(
            painter,
            vr_x - 8.0,
            vr_y - 10.0,
            34.0,
            8.0,
            "VERTICAL\n100.00 mm",
            pt=6.5,
            bold=True,
            align="center",
            color_hex=ink,
        )
        # Continuous pure-vector 100.00 mm vertical baseline segment!
        painter.setPen(_make_pen(ink, 0.40))
        painter.drawLine(QPointF(_px(vr_x), _px(vr_y)), QPointF(_px(vr_x), _px(vr_y + 100.0)))
        for mm in range(101):
            tlen = 4.5 if mm % 10 == 0 else (3.0 if mm % 5 == 0 else 1.8)
            painter.setPen(_make_pen(ink, 0.25 if mm % 10 == 0 else 0.14))
            painter.drawLine(
                QPointF(_px(vr_x), _px(vr_y + mm)),
                QPointF(_px(vr_x + tlen), _px(vr_y + mm)),
            )
            if mm % 10 == 0:
                _draw_text_box(
                    painter,
                    vr_x + 5.5,
                    vr_y + mm - 1.8,
                    12.0,
                    3.6,
                    f"{mm}",
                    pt=6.0,
                    bold=(mm in (0, 50, 100)),
                    align="left",
                    color_hex=ink,
                )

        # --- 4. TRUE-SCALE MGRS ROMER SCALE CORNERS ---
        romer_top = ir_y + 13.0
        romer_heading = (
            "MGRS ROMER SCALE CORNERS (100 m subdivisions — align 0-corner on target, read Right then Up)"
        )
        if scale <= 15000:
            km_mm_active = 1_000_000.0 / float(scale)
            romer_heading += f"  ·  1:{scale:,} active: 1 km = {km_mm_active:.2f} mm (use 100 mm ruler above)"
        _draw_text_box(
            painter,
            hr_x,
            romer_top,
            155.0,
            5.0,
            romer_heading,
            pt=6.8 if scale <= 15000 else 7.0,
            bold=True,
            color_hex=ink,
        )
        ry = romer_top + 7.5
        _draw_romer_corner(
            painter,
            hr_x,
            ry,
            40.0,
            "1:25,000",
            is_active=(scale == 25000),
            ink=ink,
            accent=accent,
            muted=muted,
        )
        _draw_romer_corner(
            painter,
            hr_x + 50.0,
            ry,
            41.6667,
            "1:24,000",
            is_active=(scale == 24000),
            ink=ink,
            accent=accent,
            muted=muted,
        )
        _draw_romer_corner(
            painter,
            hr_x + 102.0,
            ry,
            20.0,
            "1:50,000",
            is_active=(scale == 50000),
            ink=ink,
            accent=accent,
            muted=muted,
        )

        # --- 5. COMPASS PROTRACTOR REFERENCE RING (adapts cleanly to Portrait vs Landscape) ---
        if page_h >= 240.0:
            # Portrait / tall page: clear below 1:24k Romer bottom labels (~135.2 mm) & 100 mm
            # vertical ruler (~153 mm), centered comfortably above the page footer.
            romer_bottom_mm = ry + 41.67 + 4.0
            footer_top_mm = page_h - margin - 6.0
            cy_mm = min(0.5 * (romer_bottom_mm + footer_top_mm), ry + 115.0)
            cy_mm = max(cy_mm, romer_bottom_mm + 56.0)
            _draw_protractor_ring(
                painter,
                cx_mm=page_w / 2.0,
                cy_mm=cy_mm,
                radius_mm=34.0,
                ink=ink,
                muted=muted,
            )
        else:
            # Landscape page: center protractor cleanly in right column beside vertical ruler
            col_left = vr_x + 18.0
            col_right = page_w - margin - 4.0
            rad_mm = min(30.0, max(22.0, 0.5 * (col_right - col_left) - 12.0))
            _draw_protractor_ring(
                painter,
                cx_mm=0.5 * (col_left + col_right),
                cy_mm=max(margin + 38.0 + rad_mm + 8.0, page_h / 2.0 + 10.0),
                radius_mm=rad_mm,
                ink=ink,
                muted=muted,
            )

        # Footer
        _draw_text_box(
            painter,
            margin + 4.0,
            page_h - margin - 6.0,
            inner_w - 8.0,
            5.0,
            f"GridForge Scale Calibration & MGRS Romer Sheet  |  Page {page_w:.1f} × {page_h:.1f} mm  |  Print at 100% Actual Size",
            pt=6.2,
            align="center",
            color_hex=muted,
        )
    finally:
        painter.end()


def _render_route_card_in_process(spec: dict[str, Any], output_pdf: Path) -> None:
    from PyQt5.QtCore import QPointF, QRectF
    from PyQt5.QtGui import QColor, QPainter

    metrics = RouteMetrics.model_validate(spec["metrics"])
    layout = SheetLayout.model_validate(spec["layout"])
    decl = DeclinationInfo.model_validate(spec["declination"])
    bw = bool(spec.get("bw", False))

    page_w = layout.page_width_mm
    page_h = layout.page_height_mm
    is_imp = layout.spec.units == Units.IMPERIAL
    dist_unit = "mi" if is_imp else "km"
    elev_unit = "ft" if is_imp else "m"

    ink = "#000000" if bw else "#111820"
    muted = "#333333" if bw else "#4B5563"
    stripe = "#F2F2F2" if bw else "#F5F7FA"
    border = "#222222" if bw else "#94A3B8"

    writer = _setup_pdf_writer(output_pdf, page_w, page_h)
    painter = QPainter()
    if not painter.begin(writer):
        raise RuntimeError(f"Failed to start QPainter for '{output_pdf}'.")

    try:
        margin = 12.0
        inner_w = page_w - 2.0 * margin
        title = layout.spec.title or f"SHEET {layout.gzd}"

        painter.setPen(_make_pen(ink, 0.35))
        painter.drawRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(page_h - 2.0 * margin)))

        # Header banner
        painter.fillRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(12.0)), QColor(ink))
        _draw_text_box(
            painter,
            margin + 4.0,
            margin + 1.0,
            inner_w - 8.0,
            10.0,
            f"TACTICAL ROUTE CARD & WAYPOINT LOG  —  {title.upper()}",
            pt=9.5,
            bold=True,
            color_hex="#FFFFFF",
        )

        # Declination & G-M conversion bar
        gm_dir = "E" if decl.gm_angle_deg >= 0 else "W"
        decl_dir = "E" if decl.magnetic_declination_deg >= 0 else "W"
        conv_rule = (
            f"Grid→Mag: SUBTRACT {abs(decl.gm_angle_deg):.1f}°"
            if decl.gm_angle_deg >= 0
            else f"Grid→Mag: ADD {abs(decl.gm_angle_deg):.1f}°"
        )
        summary_y = margin + 14.0
        painter.fillRect(QRectF(_px(margin + 2.0), _px(summary_y), _px(inner_w - 4.0), _px(12.0)), QColor(stripe))
        painter.setPen(_make_pen(border, 0.2))
        painter.drawRect(QRectF(_px(margin + 2.0), _px(summary_y), _px(inner_w - 4.0), _px(12.0)))
        _draw_text_box(
            painter,
            margin + 5.0,
            summary_y + 1.0,
            inner_w - 10.0,
            5.0,
            f"SCALE 1:{layout.spec.scale:,}  |  UTM ZONE {layout.utm_zone}{layout.utm_hemisphere} (EPSG:{layout.utm_epsg})  |  "
            f"{decl.model_name} ({decl.decimal_year:.1f}): Decl {abs(decl.magnetic_declination_deg):.1f}° {decl_dir}, "
            f"G-M {abs(decl.gm_angle_deg):.1f}° {gm_dir} ({round(abs(decl.gm_angle_mils))} mils)",
            pt=7.0,
            bold=True,
            color_hex=ink,
        )
        total_dist_str = (
            f"{metrics.total_distance_mi:.2f} mi ({metrics.total_distance_km:.2f} km)"
            if is_imp
            else f"{metrics.total_distance_km:.2f} km ({metrics.total_distance_mi:.2f} mi)"
        )
        _draw_text_box(
            painter,
            margin + 5.0,
            summary_y + 6.2,
            inner_w - 10.0,
            5.0,
            f"TC 3-25.26 {conv_rule}   |   WAYPOINTS: {len(metrics.waypoints)}   |   TOTAL ROUTE: {total_dist_str}",
            pt=6.8,
            color_hex=muted,
        )

        # Waypoint & Leg Table
        tbl_y = summary_y + 16.0
        tbl_x = margin + 2.0
        tbl_w = inner_w - 4.0

        # Proportional column definitions summing to 1.0
        cols = [
            ("WP#", 0.065, "center"),
            ("NAME", 0.185, "left"),
            ("TYPE", 0.085, "center"),
            ("MGRS (10 m)", 0.175, "left"),
            ("LAT / LON", 0.155, "left"),
            (f"ELEV ({elev_unit})", 0.070, "right"),
            (f"LEG ({dist_unit})", 0.075, "right"),
            ("GRID AZ", 0.075, "right"),
            ("MAG AZ (° / mils)", 0.115, "right"),
        ]

        row_h = 8.0
        painter.fillRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(row_h)), QColor(ink))
        cx = tbl_x
        for hdr, frac, align in cols:
            cw = tbl_w * frac
            _draw_text_box(
                painter, cx + 1.0, tbl_y, cw - 2.0, row_h, hdr, pt=6.4, bold=True, color_hex="#FFFFFF", align=align
            )
            cx += cw

        cur_y = tbl_y + row_h
        max_rows_y = page_h - margin - 14.0
        for row_idx, wp in enumerate(metrics.waypoints):
            if cur_y + row_h > max_rows_y:
                break
            if row_idx % 2 == 1:
                painter.fillRect(QRectF(_px(tbl_x), _px(cur_y), _px(tbl_w), _px(row_h)), QColor(stripe))

            elev_txt = (
                f"{round(wp.elev_m * (METERS_TO_FEET if is_imp else 1.0)):,}"
                if wp.elev_m is not None
                else "—"
            )
            if wp.leg_dist_km is not None:
                leg_val = wp.leg_dist_mi if is_imp else wp.leg_dist_km
                leg_txt = f"{leg_val:.2f}"
                grid_txt = f"{wp.grid_az_deg:05.1f}°" if wp.grid_az_deg is not None else "—"
                mag_txt = (
                    f"{wp.mag_az_deg:05.1f}° / {int(round(wp.mag_az_mils or 0)):04d}"
                    if wp.mag_az_deg is not None
                    else "—"
                )
            else:
                leg_txt = "START"
                grid_txt = "—"
                mag_txt = "—"

            vals = [
                (wp.code, True, True),
                (wp.label[:28], True, False),
                (wp.category.value.upper(), False, False),
                (wp.mgrs_10m, False, True),
                (f"{wp.lat:.4f}, {wp.lon:.4f}", False, True),
                (elev_txt, False, True),
                (leg_txt, False, True),
                (grid_txt, False, True),
                (mag_txt, True, True),
            ]

            cx = tbl_x
            for (val_txt, is_b, is_m), (_, frac, align) in zip(vals, cols):
                cw = tbl_w * frac
                _draw_text_box(
                    painter,
                    cx + 1.0,
                    cur_y,
                    cw - 2.0,
                    row_h,
                    val_txt,
                    pt=6.3,
                    bold=is_b,
                    mono=is_m,
                    color_hex=ink,
                    align=align,
                )
                cx += cw

            painter.setPen(_make_pen(border, 0.15))
            painter.drawLine(QPointF(_px(tbl_x), _px(cur_y + row_h)), QPointF(_px(tbl_x + tbl_w), _px(cur_y + row_h)))
            cur_y += row_h

        # Outer table frame
        painter.setPen(_make_pen(ink, 0.25))
        painter.drawRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(cur_y - tbl_y)))

        # Footer
        _draw_text_box(
            painter,
            margin + 4.0,
            page_h - margin - 6.0,
            inner_w - 8.0,
            5.0,
            f"GridForge Tactical Route Card  |  WMM2025 Magnetic Azimuths  |  Total Distance: {total_dist_str}",
            pt=6.2,
            align="center",
            color_hex=muted,
        )
    finally:
        painter.end()


def _render_elevation_profile_in_process(spec: dict[str, Any], output_pdf: Path) -> None:
    from PyQt5.QtCore import QPointF, QRectF
    from PyQt5.QtGui import QColor, QPainter, QPolygonF

    profile = ElevationProfileData.model_validate(spec["profile"])
    strip_plan = (
        StripAtlasPlan.model_validate(spec["strip_plan"])
        if spec.get("strip_plan") is not None
        else None
    )
    layout = SheetLayout.model_validate(spec["layout"])
    bw = bool(spec.get("bw", False))

    page_w = layout.page_width_mm
    page_h = layout.page_height_mm
    is_imp = layout.spec.units == Units.IMPERIAL
    dist_factor = KM_TO_MILES if is_imp else 1.0
    elev_factor = METERS_TO_FEET if is_imp else 1.0
    dist_unit = "mi" if is_imp else "km"
    elev_unit = "ft" if is_imp else "m"

    ink = "#000000" if bw else "#111820"
    muted = "#333333" if bw else "#4B5563"
    stripe = "#F2F2F2" if bw else "#F5F7FA"
    fill_col = "#DCDCDC" if bw else "#CFE3C8"
    line_col = "#000000" if bw else "#245A34"
    accent = "#000000" if bw else "#B83B28"
    border = "#222222" if bw else "#94A3B8"

    writer = _setup_pdf_writer(output_pdf, page_w, page_h)
    painter = QPainter()
    if not painter.begin(writer):
        raise RuntimeError(f"Failed to start QPainter for '{output_pdf}'.")

    try:
        margin = 12.0
        inner_w = page_w - 2.0 * margin
        title = layout.spec.title or f"ROUTE STRIP ATLAS ({layout.gzd})"

        painter.setPen(_make_pen(ink, 0.35))
        painter.drawRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(page_h - 2.0 * margin)))

        # Header banner
        painter.fillRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(12.0)), QColor(ink))
        _draw_text_box(
            painter,
            margin + 4.0,
            margin + 1.0,
            inner_w - 8.0,
            10.0,
            f"ROUTE OVERVIEW & ELEVATION PROFILE  —  {title.upper()}",
            pt=9.5,
            bold=True,
            color_hex="#FFFFFF",
        )

        # 4 Key Metric Cards
        cards_y = margin + 15.0
        card_h = 16.0
        gap = 3.0
        card_w = (inner_w - 4.0 - 3.0 * gap) / 4.0
        cards = [
            ("TOTAL DISTANCE", f"{profile.total_distance_km * dist_factor:.2f} {dist_unit}"),
            ("TOTAL ASCENT", f"+{round(profile.total_ascent_m * elev_factor):,} {elev_unit}"),
            ("TOTAL DESCENT", f"-{round(profile.total_descent_m * elev_factor):,} {elev_unit}"),
            (
                "ELEVATION RANGE",
                f"{round(profile.min_elev_m * elev_factor):,} – {round(profile.max_elev_m * elev_factor):,} {elev_unit}",
            ),
        ]
        for idx, (lbl, val) in enumerate(cards):
            cx = margin + 2.0 + idx * (card_w + gap)
            painter.fillRect(QRectF(_px(cx), _px(cards_y), _px(card_w), _px(card_h)), QColor(stripe))
            painter.setPen(_make_pen(border, 0.2))
            painter.drawRect(QRectF(_px(cx), _px(cards_y), _px(card_w), _px(card_h)))
            _draw_text_box(painter, cx + 2.0, cards_y + 1.5, card_w - 4.0, 4.5, lbl, pt=6.0, bold=True, color_hex=muted)
            _draw_text_box(painter, cx + 2.0, cards_y + 7.0, card_w - 4.0, 7.5, val, pt=9.5, bold=True, color_hex=ink)

        # Vector Elevation Chart (pure crisp QPainter paths + selectable text)
        chart_x = margin + 16.0
        chart_y = cards_y + card_h + 10.0
        chart_w = inner_w - 22.0
        chart_h = 62.0 if page_h >= 240.0 else 48.0

        dists = [d * dist_factor for d in profile.distances_km]
        elevs = [e * elev_factor for e in profile.elevations_m]
        max_d = max(dists[-1] if dists else 1.0, 0.01)
        min_e = min(elevs) if elevs else 0.0
        max_e = max(elevs) if elevs else 100.0
        e_span = max(max_e - min_e, 10.0)
        y_min = math.floor((min_e - e_span * 0.12) / 10.0) * 10.0
        y_max = math.ceil((max_e + e_span * 0.15) / 10.0) * 10.0
        if y_max <= y_min:
            y_max = y_min + 20.0

        def _cx(d: float) -> float:
            return chart_x + (d / max_d) * chart_w

        def _cy(e: float) -> float:
            return chart_y + chart_h - ((e - y_min) / (y_max - y_min)) * chart_h

        # Horizontal & vertical gridlines
        for i in range(5):
            ev = y_min + (y_max - y_min) * i / 4.0
            yy = _cy(ev)
            painter.setPen(_make_pen("#D0D7DE", 0.15))
            painter.drawLine(QPointF(_px(chart_x), _px(yy)), QPointF(_px(chart_x + chart_w), _px(yy)))
            _draw_text_box(
                painter,
                margin + 1.0,
                yy - 2.0,
                13.5,
                4.0,
                f"{round(ev):,}",
                pt=6.0,
                align="right",
                color_hex=muted,
            )

        for i in range(6):
            dv = max_d * i / 5.0
            xx = _cx(dv)
            painter.setPen(_make_pen("#D0D7DE", 0.12, dashed=True))
            painter.drawLine(QPointF(_px(xx), _px(chart_y)), QPointF(_px(xx), _px(chart_y + chart_h)))
            _draw_text_box(
                painter,
                xx - 8.0,
                chart_y + chart_h + 1.2,
                16.0,
                4.0,
                f"{dv:.1f} {dist_unit}",
                pt=6.0,
                align="center",
                color_hex=muted,
            )

        if dists and elevs:
            poly = QPolygonF()
            poly.append(QPointF(_px(_cx(dists[0])), _px(chart_y + chart_h)))
            for d, e in zip(dists, elevs):
                poly.append(QPointF(_px(_cx(d)), _px(_cy(e))))
            poly.append(QPointF(_px(_cx(dists[-1])), _px(chart_y + chart_h)))
            painter.setPen(_make_pen("transparent", 0.0))
            painter.setBrush(QColor(fill_col))
            painter.drawPolygon(poly)
            painter.setBrush(QColor(0, 0, 0, 0))

            painter.setPen(_make_pen(line_col, 0.55))
            for (d0, e0), (d1, e1) in zip(zip(dists, elevs), zip(dists[1:], elevs[1:])):
                painter.drawLine(QPointF(_px(_cx(d0)), _px(_cy(e0))), QPointF(_px(_cx(d1)), _px(_cy(e1))))

            # Max/Min markers
            hi_i = max(range(len(elevs)), key=lambda i: elevs[i])
            lo_i = min(range(len(elevs)), key=lambda i: elevs[i])
            for idx_pt, tag, dy in ((hi_i, "MAX", -5.2), (lo_i, "MIN", 1.8)):
                px_mm = _cx(dists[idx_pt])
                py_mm = _cy(elevs[idx_pt])
                painter.setPen(_make_pen(accent, 0.25))
                painter.setBrush(QColor(accent))
                painter.drawEllipse(QPointF(_px(px_mm), _px(py_mm)), _px(1.0), _px(1.0))
                painter.setBrush(QColor(0, 0, 0, 0))
                _draw_text_box(
                    painter,
                    max(chart_x, min(chart_x + chart_w - 28.0, px_mm - 14.0)),
                    max(chart_y + 0.5, min(chart_y + chart_h - 4.5, py_mm + dy)),
                    28.0,
                    4.0,
                    f"{tag} {round(elevs[idx_pt]):,} {elev_unit}",
                    pt=6.0,
                    bold=True,
                    align="center",
                    color_hex=accent,
                )

        painter.setPen(_make_pen(ink, 0.30))
        painter.drawRect(QRectF(_px(chart_x), _px(chart_y), _px(chart_w), _px(chart_h)))

        # Optional Strip Atlas Sheet Index Table
        if strip_plan is not None and strip_plan.sheets:
            tbl_y = chart_y + chart_h + 11.0
            tbl_x = margin + 2.0
            tbl_w = inner_w - 4.0
            sheet_word = "SHEET" if strip_plan.sheet_count == 1 else "SHEETS"
            _draw_text_box(
                painter,
                tbl_x,
                tbl_y - 5.5,
                tbl_w,
                4.8,
                f"STRIP ATLAS SHEET INDEX  ({strip_plan.sheet_count} {sheet_word}  |  1:{strip_plan.scale:,}  |  UTM {strip_plan.utm_zone}{strip_plan.utm_hemisphere} EPSG:{strip_plan.utm_epsg})",
                pt=7.2,
                bold=True,
                color_hex=ink,
            )
            cols = [
                ("SHEET", 0.10, "center"),
                ("PAGE", 0.10, "center"),
                ("CENTER MGRS (10 m)", 0.28, "left"),
                ("CENTER LAT / LON", 0.24, "left"),
                ("UTM FOOTPRINT (E / N)", 0.28, "left"),
            ]
            row_h = 7.0
            painter.fillRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(row_h)), QColor(ink))
            cx = tbl_x
            for hdr, frac, align in cols:
                cw = tbl_w * frac
                _draw_text_box(
                    painter, cx + 1.0, tbl_y, cw - 2.0, row_h, hdr, pt=6.4, bold=True, color_hex="#FFFFFF", align=align
                )
                cx += cw

            cur_y = tbl_y + row_h
            max_y = page_h - margin - 12.0
            for idx_s, sh in enumerate(strip_plan.sheets):
                if cur_y + row_h > max_y:
                    break
                if idx_s % 2 == 1:
                    painter.fillRect(QRectF(_px(tbl_x), _px(cur_y), _px(tbl_w), _px(row_h)), QColor(stripe))
                vals = [
                    (sh.sheet_id, True, True),
                    (f"Page {sh.index + 1}", False, False),
                    (sh.center_mgrs_10m, True, True),
                    (f"{sh.center_lat:.5f}, {sh.center_lon:.5f}", False, True),
                    (
                        f"E {int(sh.utm_bbox.min_x)}–{int(sh.utm_bbox.max_x)} | N {int(sh.utm_bbox.min_y)}–{int(sh.utm_bbox.max_y)}",
                        False,
                        True,
                    ),
                ]
                cx = tbl_x
                for (txt, is_b, is_m), (_, frac, align) in zip(vals, cols):
                    cw = tbl_w * frac
                    _draw_text_box(
                        painter, cx + 1.0, cur_y, cw - 2.0, row_h, txt, pt=6.1, bold=is_b, mono=is_m, color_hex=ink, align=align
                    )
                    cx += cw
                painter.setPen(_make_pen(border, 0.15))
                painter.drawLine(QPointF(_px(tbl_x), _px(cur_y + row_h)), QPointF(_px(tbl_x + tbl_w), _px(cur_y + row_h)))
                cur_y += row_h

            painter.setPen(_make_pen(ink, 0.25))
            painter.drawRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(cur_y - tbl_y)))

        _draw_text_box(
            painter,
            margin + 4.0,
            page_h - margin - 6.0,
            inner_w - 8.0,
            5.0,
            f"GridForge Route Overview & Elevation Profile  |  Scale 1:{layout.spec.scale:,}  |  EPSG:{layout.utm_epsg} ({layout.gzd})",
            pt=6.2,
            align="center",
            color_hex=muted,
        )
    finally:
        painter.end()


def _pick_nice_step_m(span_m: float, target_ticks: int = 5) -> float:
    raw = max(span_m, 100.0) / max(1, target_ticks)
    for step in (500.0, 1000.0, 2000.0, 5000.0, 10000.0, 20000.0, 50000.0, 100000.0):
        if step >= raw:
            return step
    return 100000.0


def _pick_scalebar_distance_m(mm_per_m: float, min_mm: float = 18.0, max_mm: float = 54.0) -> float:
    candidates = (250.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0, 20000.0, 50000.0, 100000.0)
    best = candidates[0]
    for d in candidates:
        length_mm = d * mm_per_m
        if min_mm <= length_mm <= max_mm:
            return d
        if length_mm < max_mm:
            best = d
    return best


def _render_atlas_index_in_process(spec: dict[str, Any], output_pdf: Path) -> None:
    from PyQt5.QtCore import QPointF, QRectF, Qt
    from PyQt5.QtGui import QBrush, QColor, QPainter, QPainterPath, QPolygonF

    sheets = [AtlasIndexSheet.model_validate(s) for s in spec["sheets"]]
    page_w = float(spec["page_width_mm"])
    page_h = float(spec["page_height_mm"])
    scale = int(spec["scale"])
    utm_epsg = int(spec["utm_epsg"])
    title = str(spec.get("title") or "GRIDFORGE ATLAS")
    overlap_pct = float(spec.get("overlap_pct") or 0.0)
    raw_route = spec.get("route_utm") or []
    route_utm: list[tuple[float, float]] = [(float(pt[0]), float(pt[1])) for pt in raw_route]
    bw = bool(spec.get("bw", False))

    ink = "#000000" if bw else "#111820"
    muted = "#333333" if bw else "#4B5563"
    stripe = "#F2F2F2" if bw else "#F5F7FA"
    border = "#222222" if bw else "#94A3B8"
    sheet_stroke = "#111111" if bw else "#1B4F72"
    sheet_fill = QColor(235, 235, 235, 115) if bw else QColor(214, 234, 248, 115)
    overlap_color = QColor(35, 35, 35, 95) if bw else QColor(27, 79, 114, 95)
    route_col = "#000000" if bw else "#C0392B"
    start_col = "#FFFFFF" if bw else "#1E8449"
    end_col = "#000000" if bw else "#C0392B"
    canvas_bg = "#FFFFFF" if bw else "#FAFBFD"

    all_min_x = min(s.utm_bbox.min_x for s in sheets)
    all_max_x = max(s.utm_bbox.max_x for s in sheets)
    all_min_y = min(s.utm_bbox.min_y for s in sheets)
    all_max_y = max(s.utm_bbox.max_y for s in sheets)
    cov_w_km = max(0.01, (all_max_x - all_min_x) / 1000.0)
    cov_h_km = max(0.01, (all_max_y - all_min_y) / 1000.0)

    view_min_x, view_max_x = all_min_x, all_max_x
    view_min_y, view_max_y = all_min_y, all_max_y
    for rx, ry in route_utm:
        view_min_x = min(view_min_x, rx)
        view_max_x = max(view_max_x, rx)
        view_min_y = min(view_min_y, ry)
        view_max_y = max(view_max_y, ry)

    span_x = max(view_max_x - view_min_x, 250.0)
    span_y = max(view_max_y - view_min_y, 250.0)

    writer = _setup_pdf_writer(output_pdf, page_w, page_h)
    painter = QPainter()
    if not painter.begin(writer):
        raise RuntimeError(f"Failed to start QPainter for '{output_pdf}'.")

    try:
        margin = 12.0
        inner_w = page_w - 2.0 * margin
        sheet_word = "SHEET" if len(sheets) == 1 else "SHEETS"

        # Outer page border
        painter.setPen(_make_pen(ink, 0.35))
        painter.drawRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(page_h - 2.0 * margin)))

        # Header banner
        painter.fillRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(12.0)), QColor(ink))
        _draw_text_box(
            painter,
            margin + 4.0,
            margin + 1.0,
            inner_w - 8.0,
            10.0,
            f"ATLAS OVERVIEW & SHEET INDEX  —  {title.upper()}",
            pt=9.5,
            bold=True,
            color_hex="#FFFFFF",
        )

        # Telemetry bar
        summary_y = margin + 14.0
        summary_h = 10.5
        painter.fillRect(
            QRectF(_px(margin + 2.0), _px(summary_y), _px(inner_w - 4.0), _px(summary_h)),
            QColor(stripe),
        )
        painter.setPen(_make_pen(border, 0.2))
        painter.drawRect(
            QRectF(_px(margin + 2.0), _px(summary_y), _px(inner_w - 4.0), _px(summary_h))
        )

        overlap_txt = f"  |  OVERLAP: {overlap_pct:.0f}%" if overlap_pct > 0.0 else ""
        _draw_text_box(
            painter,
            margin + 5.0,
            summary_y + 1.0,
            inner_w * 0.64,
            8.5,
            f"{len(sheets)} {sheet_word}  |  SCALE 1:{scale:,}  |  UTM EPSG:{utm_epsg}{overlap_txt}",
            pt=7.2,
            bold=True,
            color_hex=ink,
        )
        _draw_text_box(
            painter,
            margin + inner_w * 0.64,
            summary_y + 1.0,
            inner_w * 0.36 - 5.0,
            8.5,
            f"COVERAGE: {cov_w_km:.1f} × {cov_h_km:.1f} km",
            pt=7.2,
            bold=True,
            align="right",
            color_hex=ink,
        )

        # Dynamic vertical budget: schematic overview map box + full index table
        footer_y = page_h - margin - 6.0
        usable_bottom = footer_y - 2.5
        map_x = margin + 2.0
        map_y = summary_y + summary_h + 3.5
        map_w = inner_w - 4.0
        avail_total = usable_bottom - map_y

        is_landscape = page_w > page_h
        if len(sheets) <= 6 and not is_landscape:
            row_h = 6.8
        elif len(sheets) <= 9 and not is_landscape:
            row_h = 6.0
        elif len(sheets) <= 8:
            row_h = 5.6
        else:
            row_h = 4.9

        tbl_header_h = 6.8
        tbl_section_gap = 7.5
        needed_table_h = tbl_section_gap + tbl_header_h + len(sheets) * row_h + 2.0
        max_map_h = min(132.0 if not is_landscape else 92.0, avail_total * 0.62)
        min_map_h = 50.0 if is_landscape else 64.0
        map_h = max(min_map_h, min(max_map_h, avail_total - needed_table_h))

        # --- 1. TRUE-PROPORTION SCHEMATIC OVERVIEW MAP BOX ---
        painter.fillRect(QRectF(_px(map_x), _px(map_y), _px(map_w), _px(map_h)), QColor(canvas_bg))

        pad_l = 16.0
        pad_r = 18.0
        pad_t = 9.0
        pad_b = 14.5
        draw_w = max(20.0, map_w - pad_l - pad_r)
        draw_h = max(20.0, map_h - pad_t - pad_b)

        mm_per_m = min(draw_w / span_x, draw_h / span_y)
        used_w = span_x * mm_per_m
        used_h = span_y * mm_per_m
        origin_x = map_x + pad_l + (draw_w - used_w) * 0.5
        origin_y = map_y + pad_t + (draw_h - used_h) * 0.5

        def _sx(e: float) -> float:
            return origin_x + (e - view_min_x) * mm_per_m

        def _sy(n: float) -> float:
            return origin_y + (view_max_y - n) * mm_per_m

        # Clip coordinate grid & sheet boxes cleanly inside map_x..map_x+map_w
        painter.save()
        painter.setClipRect(QRectF(_px(map_x + 0.3), _px(map_y + 0.3), _px(map_w - 0.6), _px(map_h - 0.6)))

        # Subtle UTM reference gridlines
        grid_step_m = _pick_nice_step_m(max(span_x, span_y), target_ticks=5)
        e_grid_min = view_min_x - (origin_x - map_x) / mm_per_m
        e_grid_max = view_min_x + (map_x + map_w - origin_x) / mm_per_m
        n_grid_min = view_max_y - (map_y + map_h - origin_y) / mm_per_m
        n_grid_max = view_max_y + (origin_y - map_y) / mm_per_m

        e0 = math.ceil(e_grid_min / grid_step_m) * grid_step_m
        e_cur = e0
        while e_cur <= e_grid_max:
            gx = _sx(e_cur)
            if map_x + 12.0 <= gx <= map_x + map_w - 14.0:
                painter.setPen(_make_pen("#E2E8F0" if not bw else "#D6D6D6", 0.12))
                painter.drawLine(QPointF(_px(gx), _px(map_y)), QPointF(_px(gx), _px(map_y + map_h)))
                _draw_text_box(
                    painter,
                    gx - 10.0,
                    map_y + 0.8,
                    20.0,
                    3.6,
                    f"{int(round(e_cur / 1000.0))}k E",
                    pt=5.0,
                    mono=True,
                    align="center",
                    color_hex=muted,
                )
            e_cur += grid_step_m

        n0 = math.ceil(n_grid_min / grid_step_m) * grid_step_m
        n_cur = n0
        while n_cur <= n_grid_max:
            gy = _sy(n_cur)
            if map_y + 5.0 <= gy <= map_y + map_h - 11.0:
                painter.setPen(_make_pen("#E2E8F0" if not bw else "#D6D6D6", 0.12))
                painter.drawLine(QPointF(_px(map_x), _px(gy)), QPointF(_px(map_x + map_w), _px(gy)))
                _draw_text_box(
                    painter,
                    map_x + 1.0,
                    gy - 1.8,
                    13.5,
                    3.6,
                    f"{int(round(n_cur / 1000.0))}k N",
                    pt=5.0,
                    mono=True,
                    align="left",
                    color_hex=muted,
                )
            n_cur += grid_step_m

        # Sheet rectangles
        for sh in sheets:
            rx0 = _sx(sh.utm_bbox.min_x)
            ry0 = _sy(sh.utm_bbox.max_y)
            rw = sh.utm_bbox.width * mm_per_m
            rh = sh.utm_bbox.height * mm_per_m
            rect = QRectF(_px(rx0), _px(ry0), _px(rw), _px(rh))
            painter.fillRect(rect, sheet_fill)
            painter.setPen(_make_pen(sheet_stroke, 0.36))
            painter.drawRect(rect)

        # Pairwise overlap hatching
        has_overlap_hatch = False
        hatch_brush = QBrush(overlap_color, Qt.BDiagPattern)
        for i in range(len(sheets)):
            a = sheets[i].utm_bbox
            for j in range(i + 1, len(sheets)):
                b = sheets[j].utm_bbox
                ix0 = max(a.min_x, b.min_x)
                ix1 = min(a.max_x, b.max_x)
                iy0 = max(a.min_y, b.min_y)
                iy1 = min(a.max_y, b.max_y)
                if (ix1 - ix0) > 1.0 and (iy1 - iy0) > 1.0:
                    has_overlap_hatch = True
                    orect = QRectF(
                        _px(_sx(ix0)),
                        _px(_sy(iy1)),
                        _px((ix1 - ix0) * mm_per_m),
                        _px((iy1 - iy0) * mm_per_m),
                    )
                    painter.fillRect(orect, hatch_brush)
                    painter.setPen(_make_pen(sheet_stroke, 0.18, dashed=True))
                    painter.drawRect(orect)

        # Explicit overlap_pct inset hatch band when grid sheets touch without geometric overlap
        if overlap_pct > 0.0 and not has_overlap_hatch:
            has_overlap_hatch = True
            frac = min(0.20, max(0.02, overlap_pct / 200.0))
            for sh in sheets:
                b = sh.utm_bbox
                dx = b.width * frac
                dy = b.height * frac
                inner_rect = QRectF(
                    _px(_sx(b.min_x + dx)),
                    _px(_sy(b.max_y - dy)),
                    _px((b.width - 2.0 * dx) * mm_per_m),
                    _px((b.height - 2.0 * dy) * mm_per_m),
                )
                painter.setPen(_make_pen(sheet_stroke, 0.16, dashed=True))
                painter.drawRect(inner_rect)

        # Optional GPX / GeoJSON route polyline drawn right through the sheet boxes
        if len(route_utm) >= 2:
            path = QPainterPath()
            path.moveTo(_px(_sx(route_utm[0][0])), _px(_sy(route_utm[0][1])))
            for rx, ry in route_utm[1:]:
                path.lineTo(_px(_sx(rx)), _px(_sy(ry)))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(_make_pen("#FFFFFF", 1.15))
            painter.drawPath(path)
            painter.setPen(_make_pen(route_col, 0.58, dashed=bw))
            painter.drawPath(path)

            # Start marker (circle) & Finish marker (square)
            st_x, st_y = _sx(route_utm[0][0]), _sy(route_utm[0][1])
            en_x, en_y = _sx(route_utm[-1][0]), _sy(route_utm[-1][1])
            painter.setPen(_make_pen(ink, 0.25))
            painter.setBrush(QColor(start_col))
            painter.drawEllipse(QPointF(_px(st_x), _px(st_y)), _px(1.3), _px(1.3))
            painter.setBrush(QColor(end_col))
            painter.drawRect(QRectF(_px(en_x - 1.2), _px(en_y - 1.2), _px(2.4), _px(2.4)))
            painter.setBrush(Qt.NoBrush)

        # Bold centered sheet IDs on crisp white pills
        for sh in sheets:
            cx = _sx((sh.utm_bbox.min_x + sh.utm_bbox.max_x) * 0.5)
            cy = _sy((sh.utm_bbox.min_y + sh.utm_bbox.max_y) * 0.5)
            sw_mm = sh.utm_bbox.width * mm_per_m
            sh_mm = sh.utm_bbox.height * mm_per_m

            pill_w = min(max(10.0, len(sh.sheet_id) * 3.4 + 4.0), max(8.0, sw_mm - 1.5))
            show_sub = sh_mm >= 16.0 and sw_mm >= 14.0
            pill_h = 8.4 if show_sub else 5.4
            pill_rect = QRectF(_px(cx - pill_w * 0.5), _px(cy - pill_h * 0.5), _px(pill_w), _px(pill_h))
            painter.fillRect(pill_rect, QColor(255, 255, 255, 225))
            painter.setPen(_make_pen(sheet_stroke, 0.18))
            painter.drawRect(pill_rect)

            id_pt = 8.4 if sw_mm >= 22.0 else (7.0 if sw_mm >= 14.0 else 6.0)
            if show_sub:
                _draw_text_box(
                    painter,
                    cx - pill_w * 0.5,
                    cy - pill_h * 0.5 + 0.3,
                    pill_w,
                    4.6,
                    sh.sheet_id,
                    pt=id_pt,
                    bold=True,
                    mono=True,
                    align="center",
                    color_hex=ink,
                )
                _draw_text_box(
                    painter,
                    cx - pill_w * 0.5,
                    cy + 0.4,
                    pill_w,
                    3.6,
                    f"p.{sh.page_number}",
                    pt=5.2,
                    align="center",
                    color_hex=muted,
                )
            else:
                _draw_text_box(
                    painter,
                    cx - pill_w * 0.5,
                    cy - pill_h * 0.5,
                    pill_w,
                    pill_h,
                    sh.sheet_id,
                    pt=id_pt,
                    bold=True,
                    mono=True,
                    align="center",
                    color_hex=ink,
                )

        # North Arrow badge (top-right inside map box)
        na_w, na_h = 11.0, 15.0
        na_x = map_x + map_w - na_w - 2.5
        na_y = map_y + 2.5
        painter.fillRect(QRectF(_px(na_x), _px(na_y), _px(na_w), _px(na_h)), QColor(255, 255, 255, 235))
        painter.setPen(_make_pen(border, 0.18))
        painter.drawRect(QRectF(_px(na_x), _px(na_y), _px(na_w), _px(na_h)))
        _draw_text_box(
            painter, na_x, na_y + 0.4, na_w, 4.2, "N", pt=7.0, bold=True, align="center", color_hex=ink
        )
        ax = na_x + na_w * 0.5
        ay_top = na_y + 5.0
        ay_bot = na_y + 13.5
        left_tri = QPolygonF(
            [
                QPointF(_px(ax), _px(ay_top)),
                QPointF(_px(ax - 2.4), _px(ay_bot)),
                QPointF(_px(ax), _px(ay_bot - 2.0)),
            ]
        )
        right_tri = QPolygonF(
            [
                QPointF(_px(ax), _px(ay_top)),
                QPointF(_px(ax + 2.4), _px(ay_bot)),
                QPointF(_px(ax), _px(ay_bot - 2.0)),
            ]
        )
        painter.setPen(_make_pen(ink, 0.2))
        painter.setBrush(QColor(ink))
        painter.drawPolygon(left_tri)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawPolygon(right_tri)
        painter.setBrush(Qt.NoBrush)

        # True-proportion schematic scale bar (bottom-left inside map box)
        sb_dist_m = _pick_scalebar_distance_m(mm_per_m)
        sb_len_mm = sb_dist_m * mm_per_m
        sb_badge_w = max(28.0, sb_len_mm + 14.0)
        sb_badge_h = 8.8
        sb_x = map_x + 2.5
        sb_y = map_y + map_h - sb_badge_h - 2.0
        painter.fillRect(
            QRectF(_px(sb_x), _px(sb_y), _px(sb_badge_w), _px(sb_badge_h)),
            QColor(255, 255, 255, 235),
        )
        painter.setPen(_make_pen(border, 0.18))
        painter.drawRect(QRectF(_px(sb_x), _px(sb_y), _px(sb_badge_w), _px(sb_badge_h)))

        bar_x = sb_x + 4.0
        bar_y = sb_y + 4.8
        bar_h = 2.0
        half_len = sb_len_mm * 0.5
        painter.fillRect(QRectF(_px(bar_x), _px(bar_y), _px(half_len), _px(bar_h)), QColor(ink))
        painter.fillRect(
            QRectF(_px(bar_x + half_len), _px(bar_y), _px(half_len), _px(bar_h)),
            QColor("#FFFFFF"),
        )
        painter.setPen(_make_pen(ink, 0.22))
        painter.drawRect(QRectF(_px(bar_x), _px(bar_y), _px(sb_len_mm), _px(bar_h)))
        dist_lbl = f"{int(sb_dist_m)} m" if sb_dist_m < 1000.0 else f"{sb_dist_m / 1000.0:g} km"
        _draw_text_box(painter, bar_x - 2.0, sb_y + 0.6, 8.0, 3.6, "0", pt=5.4, bold=True, color_hex=ink)
        _draw_text_box(
            painter,
            bar_x + sb_len_mm - 12.0,
            sb_y + 0.6,
            18.0,
            3.6,
            dist_lbl,
            pt=5.4,
            bold=True,
            align="right",
            color_hex=ink,
        )

        # Compact legend badge (bottom-right) if route or overlap hatch is drawn
        legend_items: list[str] = []
        if len(route_utm) >= 2:
            legend_items.append("route")
        if has_overlap_hatch:
            legend_items.append("overlap")
        if legend_items:
            leg_w = 24.0 * len(legend_items) + 4.0
            leg_h = 7.4
            leg_x = map_x + map_w - leg_w - 2.5
            leg_y = map_y + map_h - leg_h - 2.0
            painter.fillRect(
                QRectF(_px(leg_x), _px(leg_y), _px(leg_w), _px(leg_h)),
                QColor(255, 255, 255, 235),
            )
            painter.setPen(_make_pen(border, 0.18))
            painter.drawRect(QRectF(_px(leg_x), _px(leg_y), _px(leg_w), _px(leg_h)))
            lx = leg_x + 3.0
            if "route" in legend_items:
                painter.setPen(_make_pen(route_col, 0.55, dashed=bw))
                painter.drawLine(QPointF(_px(lx), _px(leg_y + leg_h * 0.5)), QPointF(_px(lx + 6.0), _px(leg_y + leg_h * 0.5)))
                _draw_text_box(painter, lx + 7.2, leg_y + 1.0, 14.0, 5.4, "Route", pt=5.4, bold=True, color_hex=ink)
                lx += 23.0
            if "overlap" in legend_items:
                ov_box = QRectF(_px(lx), _px(leg_y + 1.7), _px(5.5), _px(4.0))
                painter.fillRect(ov_box, hatch_brush)
                painter.setPen(_make_pen(sheet_stroke, 0.2))
                painter.drawRect(ov_box)
                _draw_text_box(painter, lx + 6.8, leg_y + 1.0, 15.0, 5.4, "Overlap", pt=5.4, bold=True, color_hex=ink)

        painter.restore()

        # Crisp outer map box frame
        painter.setPen(_make_pen(ink, 0.30))
        painter.drawRect(QRectF(_px(map_x), _px(map_y), _px(map_w), _px(map_h)))

        # --- 2. SHEET INDEX TABLE ---
        tbl_x = map_x
        tbl_w = map_w
        tbl_title_y = map_y + map_h + 2.5
        _draw_text_box(
            painter,
            tbl_x,
            tbl_title_y,
            tbl_w,
            4.5,
            f"SHEET INDEX TABLE  ({len(sheets)} {sheet_word}  |  1:{scale:,}  |  EPSG:{utm_epsg}  |  COVERAGE {cov_w_km:.1f} × {cov_h_km:.1f} km)",
            pt=7.0,
            bold=True,
            color_hex=ink,
        )

        tbl_y = tbl_title_y + 5.0
        cols = [
            ("SHEET", 0.09, "center"),
            ("PAGE", 0.09, "center"),
            ("CENTER MGRS (10 m)", 0.26, "left"),
            ("CENTER LAT / LON", 0.22, "left"),
            ("UTM EXTENT (E / N)", 0.34, "left"),
        ]

        painter.fillRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(tbl_header_h)), QColor(ink))
        cx = tbl_x
        for hdr, frac, align in cols:
            cw = tbl_w * frac
            _draw_text_box(
                painter, cx + 1.0, tbl_y, cw - 2.0, tbl_header_h, hdr, pt=6.2, bold=True, color_hex="#FFFFFF", align=align
            )
            cx += cw

        cur_y = tbl_y + tbl_header_h
        row_pt = 6.0 if row_h >= 5.8 else 5.5
        for idx_s, sh in enumerate(sheets):
            if cur_y + row_h > usable_bottom:
                break
            if idx_s % 2 == 1:
                painter.fillRect(QRectF(_px(tbl_x), _px(cur_y), _px(tbl_w), _px(row_h)), QColor(stripe))
            vals = [
                (sh.sheet_id, True, True),
                (f"Page {sh.page_number}", False, False),
                (sh.center_mgrs_10m, True, True),
                (f"{sh.center_lat:.5f}, {sh.center_lon:.5f}", False, True),
                (
                    f"E {int(round(sh.utm_bbox.min_x))}–{int(round(sh.utm_bbox.max_x))} | "
                    f"N {int(round(sh.utm_bbox.min_y))}–{int(round(sh.utm_bbox.max_y))}",
                    False,
                    True,
                ),
            ]
            cx = tbl_x
            for (txt, is_b, is_m), (_, frac, align) in zip(vals, cols):
                cw = tbl_w * frac
                _draw_text_box(
                    painter, cx + 1.0, cur_y, cw - 2.0, row_h, txt, pt=row_pt, bold=is_b, mono=is_m, color_hex=ink, align=align
                )
                cx += cw
            painter.setPen(_make_pen(border, 0.15))
            painter.drawLine(QPointF(_px(tbl_x), _px(cur_y + row_h)), QPointF(_px(tbl_x + tbl_w), _px(cur_y + row_h)))
            cur_y += row_h

        painter.setPen(_make_pen(ink, 0.25))
        painter.drawRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(cur_y - tbl_y)))

        # Footer
        _draw_text_box(
            painter,
            margin + 4.0,
            footer_y,
            inner_w - 8.0,
            5.0,
            f"GridForge Atlas Overview & Sheet Index  |  Scale 1:{scale:,}  |  EPSG:{utm_epsg}  |  "
            f"{len(sheets)} {sheet_word.lower()} ({cov_w_km:.1f} × {cov_h_km:.1f} km)",
            pt=6.2,
            align="center",
            color_hex=muted,
        )
    finally:
        painter.end()


def _render_rf_link_card_in_process(spec: dict[str, Any], output_pdf: Path) -> None:
    from PyQt5.QtCore import QPointF, QRectF, Qt
    from PyQt5.QtGui import QColor, QFontMetricsF, QPainter, QPolygonF

    profiles: list[dict[str, Any]] = list(spec.get("profiles") or [])
    layout = SheetLayout.model_validate(spec["layout"])
    bw = bool(spec.get("bw", False))

    page_w = layout.page_width_mm
    page_h = layout.page_height_mm
    is_imp = layout.spec.units == Units.IMPERIAL
    dist_factor = KM_TO_MILES if is_imp else 1.0
    elev_factor = METERS_TO_FEET if is_imp else 1.0
    dist_unit = "mi" if is_imp else "km"
    elev_unit = "ft" if is_imp else "m"

    ink = "#000000" if bw else "#111820"
    muted = "#333333" if bw else "#4B5563"
    stripe = "#F2F2F2" if bw else "#F5F7FA"
    border = "#222222" if bw else "#94A3B8"
    terrain_fill = "#DCDCDC" if bw else "#D6E2D0"
    terrain_stroke = "#111111" if bw else "#3A5F3E"
    fresnel_fill = QColor(180, 180, 180, 75) if bw else QColor(14, 165, 233, 48)
    fresnel_stroke = "#444444" if bw else "#0284C7"
    f60_stroke = "#666666" if bw else "#D97706"

    status_colors: dict[str, str] = {
        "CLEAR": "#111111" if bw else "#15803D",
        "MARGINAL": "#333333" if bw else "#B45309",
        "OBSTRUCTED": "#000000" if bw else "#B91C1C",
    }

    writer = _setup_pdf_writer(output_pdf, page_w, page_h)
    painter = QPainter()
    if not painter.begin(writer):
        raise RuntimeError(f"Failed to start QPainter for '{output_pdf}'.")

    try:
        margin = 12.0
        inner_w = page_w - 2.0 * margin
        title = layout.spec.title or f"SHEET {layout.gzd}"

        # Outer page border
        painter.setPen(_make_pen(ink, 0.35))
        painter.drawRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(page_h - 2.0 * margin)))

        # Header banner
        painter.fillRect(QRectF(_px(margin), _px(margin), _px(inner_w), _px(12.0)), QColor(ink))
        _draw_text_box(
            painter,
            margin + 4.0,
            margin + 1.0,
            inner_w - 8.0,
            10.0,
            f"RF / LINE-OF-SIGHT LINK PROFILE & FRESNEL CARD  —  {title.upper()}",
            pt=9.2,
            bold=True,
            color_hex="#FFFFFF",
        )

        primary = profiles[0]
        p_status = str(primary.get("status") or "CLEAR").upper()
        st_col = status_colors.get(p_status, ink)
        from_lbl = str(primary.get("from_label") or "OBS1")
        to_lbl = str(primary.get("to_label") or "OBS2")
        from_mgrs = str(primary.get("from_mgrs") or "—")
        to_mgrs = str(primary.get("to_mgrs") or "—")
        from_elev = float(primary.get("from_elev_m") or 0.0) * elev_factor
        to_elev = float(primary.get("to_elev_m") or 0.0) * elev_factor
        from_h = float(primary.get("from_height_m") or 2.0)
        to_h = float(primary.get("to_height_m") or 2.0)
        dist_km = float(primary.get("distance_km") or 0.0)
        freq_mhz = float(primary.get("freq_mhz") or 915.0)
        fspl_db = float(primary.get("fspl_db") or 0.0)
        min_clr_m = float(primary.get("min_clearance_m") or 0.0)
        min_f1_pct = float(primary.get("min_fresnel_pct") or 0.0)
        b_true = float(primary.get("bearing_true_deg") or 0.0)
        b_grid = float(primary.get("bearing_grid_deg") or 0.0)
        b_mag = float(primary.get("bearing_mag_deg") or 0.0)
        b_mils = float(primary.get("bearing_mils") or 0.0)

        # Endpoint telemetry bar + status badge
        bar_y = margin + 14.0
        bar_h = 12.0
        badge_w = 32.0
        bar_x = margin + 2.0
        bar_w = inner_w - 4.0
        painter.fillRect(QRectF(_px(bar_x), _px(bar_y), _px(bar_w), _px(bar_h)), QColor(stripe))
        painter.setPen(_make_pen(border, 0.2))
        painter.drawRect(QRectF(_px(bar_x), _px(bar_y), _px(bar_w), _px(bar_h)))

        painter.fillRect(
            QRectF(_px(bar_x + bar_w - badge_w), _px(bar_y), _px(badge_w), _px(bar_h)),
            QColor(st_col),
        )
        _draw_text_box(
            painter,
            bar_x + bar_w - badge_w + 1.0,
            bar_y + 1.0,
            badge_w - 2.0,
            bar_h - 2.0,
            p_status,
            pt=8.2,
            bold=True,
            align="center",
            color_hex="#FFFFFF",
        )
        _draw_text_box(
            painter,
            bar_x + 3.0,
            bar_y + 1.0,
            bar_w - badge_w - 5.0,
            5.0,
            f"A: {from_lbl} ({from_mgrs})  ·  {round(from_elev):,} {elev_unit} + {from_h:g} m AGL",
            pt=6.8,
            bold=True,
            mono=True,
            color_hex=ink,
        )
        _draw_text_box(
            painter,
            bar_x + 3.0,
            bar_y + 6.2,
            bar_w - badge_w - 5.0,
            5.0,
            f"B: {to_lbl} ({to_mgrs})  ·  {round(to_elev):,} {elev_unit} + {to_h:g} m AGL",
            pt=6.8,
            bold=True,
            mono=True,
            color_hex=muted,
        )

        # 4 Key RF Metric Cards
        cards_y = bar_y + bar_h + 3.0
        card_h = 15.5
        gap = 2.8
        card_w = (bar_w - 3.0 * gap) / 4.0
        cards = [
            ("DISTANCE & BAND", f"{dist_km * dist_factor:.2f} {dist_unit} @ {freq_mhz:g} MHz"),
            ("FREE-SPACE LOSS", f"{fspl_db:.1f} dB"),
            (
                "MIN CLEARANCE (F1)",
                f"{min_clr_m * elev_factor:+.1f} {elev_unit} ({min_f1_pct:.0f}% F1)",
            ),
            (
                "MAG AZ / MILS",
                f"{b_mag:05.1f}°M ({int(round(b_mils)):04d}m) | {b_grid:05.1f}°G",
            ),
        ]
        for idx, (lbl, val) in enumerate(cards):
            cx = bar_x + idx * (card_w + gap)
            painter.fillRect(QRectF(_px(cx), _px(cards_y), _px(card_w), _px(card_h)), QColor(stripe))
            painter.setPen(_make_pen(border, 0.2))
            painter.drawRect(QRectF(_px(cx), _px(cards_y), _px(card_w), _px(card_h)))
            _draw_text_box(
                painter, cx + 2.0, cards_y + 1.4, card_w - 4.0, 4.2, lbl, pt=5.8, bold=True, color_hex=muted
            )
            _draw_text_box(
                painter,
                cx + 2.0,
                cards_y + 6.5,
                card_w - 4.0,
                7.2,
                val,
                pt=8.0 if len(val) <= 18 else 6.8,
                bold=True,
                color_hex=ink,
            )

        # Pure-vector Fresnel Zone & Terrain Elevation Profile Chart
        chart_x = margin + 17.0
        chart_y = cards_y + card_h + 8.5
        chart_w = inner_w - 23.0
        is_landscape = page_w > page_h
        chart_h = 74.0 if not is_landscape else 52.0

        # Split chart title cleanly across full bar_x..bar_x+bar_w so bearings never clip on portrait sheets
        left_title_w = bar_w * 0.46
        right_title_w = bar_w - left_title_w - 2.0
        _draw_text_box(
            painter,
            bar_x,
            chart_y - 5.2,
            left_title_w,
            4.5,
            "CURVED-EARTH (k=4/3) TERRAIN & 1ST FRESNEL PROFILE",
            pt=6.2,
            bold=True,
            color_hex=ink,
        )
        right_raw = (
            f"{from_lbl} → {to_lbl}  "
            f"({b_true:05.1f}°T · {b_grid:05.1f}°G · {b_mag:05.1f}°M)"
        )
        right_font = _make_font(6.0, bold=False)
        right_elided = QFontMetricsF(right_font).elidedText(
            right_raw, Qt.ElideRight, _px(right_title_w)
        )
        _draw_text_box(
            painter,
            bar_x + left_title_w + 2.0,
            chart_y - 5.2,
            right_title_w,
            4.5,
            right_elided,
            pt=6.0,
            bold=False,
            align="right",
            color_hex=muted,
        )

        samples: list[dict[str, Any]] = list(primary.get("samples") or [])
        if samples:
            xs_d = [float(s.get("dist_km", 0.0)) * dist_factor for s in samples]
            terr = [float(s.get("terrain_m", 0.0)) * elev_factor for s in samples]
            los = [float(s.get("los_m", 0.0)) * elev_factor for s in samples]
            f_lo = [float(s.get("fresnel_lo_m", s.get("los_m", 0.0))) * elev_factor for s in samples]
            f_hi = [float(s.get("fresnel_hi_m", s.get("los_m", 0.0))) * elev_factor for s in samples]
            f60_lo = [float(s.get("fresnel60_lo_m", s.get("los_m", 0.0))) * elev_factor for s in samples]
            clrs = [float(s.get("clearance_m", 0.0)) for s in samples]
        else:
            xs_d = [0.0, max(dist_km * dist_factor, 0.01)]
            terr = [from_elev, to_elev]
            los = [from_elev + from_h * elev_factor, to_elev + to_h * elev_factor]
            f_lo, f_hi, f60_lo = list(los), list(los), list(los)
            clrs = [min_clr_m, min_clr_m]

        max_d = max(xs_d[-1] if xs_d else 1.0, 0.01)
        all_y = terr + los + f_lo + f_hi
        min_y = min(all_y) if all_y else 0.0
        max_y = max(all_y) if all_y else 100.0
        y_span = max(max_y - min_y, 20.0)
        y_min = math.floor((min_y - y_span * 0.14) / 10.0) * 10.0
        y_max = math.ceil((max_y + y_span * 0.18) / 10.0) * 10.0
        if y_max <= y_min:
            y_max = y_min + 20.0

        def _cx(d: float) -> float:
            return chart_x + (d / max_d) * chart_w

        def _cy(e: float) -> float:
            return chart_y + chart_h - ((e - y_min) / (y_max - y_min)) * chart_h

        # Gridlines & axes
        for i in range(5):
            ev = y_min + (y_max - y_min) * i / 4.0
            yy = _cy(ev)
            painter.setPen(_make_pen("#D0D7DE", 0.15))
            painter.drawLine(QPointF(_px(chart_x), _px(yy)), QPointF(_px(chart_x + chart_w), _px(yy)))
            _draw_text_box(
                painter,
                margin + 1.0,
                yy - 2.0,
                14.5,
                4.0,
                f"{round(ev):,} {elev_unit}",
                pt=5.6,
                align="right",
                color_hex=muted,
            )

        for i in range(6):
            dv = max_d * i / 5.0
            xx = _cx(dv)
            painter.setPen(_make_pen("#D0D7DE", 0.12, dashed=True))
            painter.drawLine(QPointF(_px(xx), _px(chart_y)), QPointF(_px(xx), _px(chart_y + chart_h)))
            _draw_text_box(
                painter,
                xx - 8.5,
                chart_y + chart_h + 1.2,
                17.0,
                4.0,
                f"{dv:.1f} {dist_unit}",
                pt=5.8,
                align="center",
                color_hex=muted,
            )

        painter.save()
        painter.setClipRect(QRectF(_px(chart_x), _px(chart_y), _px(chart_w), _px(chart_h)))

        # 1. Terrain fill + stroke
        t_poly = QPolygonF()
        t_poly.append(QPointF(_px(_cx(xs_d[0])), _px(chart_y + chart_h)))
        for d, e in zip(xs_d, terr):
            t_poly.append(QPointF(_px(_cx(d)), _px(_cy(e))))
        t_poly.append(QPointF(_px(_cx(xs_d[-1])), _px(chart_y + chart_h)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(terrain_fill))
        painter.drawPolygon(t_poly)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(_make_pen(terrain_stroke, 0.45))
        for i in range(len(xs_d) - 1):
            painter.drawLine(
                QPointF(_px(_cx(xs_d[i])), _px(_cy(terr[i]))),
                QPointF(_px(_cx(xs_d[i + 1])), _px(_cy(terr[i + 1]))),
            )

        # 2. Outer 1st Fresnel zone envelope
        f_poly = QPolygonF()
        for d, e in zip(xs_d, f_hi):
            f_poly.append(QPointF(_px(_cx(d)), _px(_cy(e))))
        for d, e in zip(reversed(xs_d), reversed(f_lo)):
            f_poly.append(QPointF(_px(_cx(d)), _px(_cy(e))))
        painter.setPen(_make_pen(fresnel_stroke, 0.22, dashed=True))
        painter.setBrush(fresnel_fill)
        painter.drawPolygon(f_poly)
        painter.setBrush(Qt.NoBrush)

        # 3. Inner 60% Fresnel clearance curve
        painter.setPen(_make_pen(f60_stroke, 0.28, dashed=True))
        for i in range(len(xs_d) - 1):
            painter.drawLine(
                QPointF(_px(_cx(xs_d[i])), _px(_cy(f60_lo[i]))),
                QPointF(_px(_cx(xs_d[i + 1])), _px(_cy(f60_lo[i + 1]))),
            )

        # 4. Direct LOS line
        painter.setPen(_make_pen(st_col, 0.55))
        for i in range(len(xs_d) - 1):
            painter.drawLine(
                QPointF(_px(_cx(xs_d[i])), _px(_cy(los[i]))),
                QPointF(_px(_cx(xs_d[i + 1])), _px(_cy(los[i + 1]))),
            )

        # 5. Worst clearance marker (interior points preferred)
        if len(xs_d) >= 3:
            worst_i = min(range(1, len(xs_d) - 1), key=lambda k: clrs[k])
            wx = _cx(xs_d[worst_i])
            wy = _cy(terr[worst_i])
            painter.setPen(_make_pen(st_col, 0.28))
            painter.setBrush(QColor(st_col))
            painter.drawEllipse(QPointF(_px(wx), _px(wy)), _px(1.1), _px(1.1))
            painter.setBrush(Qt.NoBrush)
            _draw_text_box(
                painter,
                max(chart_x + 1.0, min(chart_x + chart_w - 34.0, wx - 17.0)),
                max(chart_y + 1.0, min(chart_y + chart_h - 5.0, wy - 5.5)),
                34.0,
                4.2,
                f"MIN {clrs[worst_i] * elev_factor:+.1f} {elev_unit}",
                pt=5.8,
                bold=True,
                align="center",
                color_hex=st_col,
            )

        # Compact legend pill inside top-right of chart
        leg_w, leg_h = 74.0, 6.2
        leg_x = chart_x + chart_w - leg_w - 2.0
        leg_y = chart_y + 1.8
        painter.fillRect(
            QRectF(_px(leg_x), _px(leg_y), _px(leg_w), _px(leg_h)),
            QColor(255, 255, 255, 230),
        )
        painter.setPen(_make_pen(border, 0.16))
        painter.drawRect(QRectF(_px(leg_x), _px(leg_y), _px(leg_w), _px(leg_h)))
        lx = leg_x + 2.5
        for col_hex, label_txt, is_dash in (
            (terrain_stroke, "Terrain", False),
            (st_col, "LOS", False),
            (fresnel_stroke, "1st F1", True),
            (f60_stroke, "60% F1", True),
        ):
            painter.setPen(_make_pen(col_hex, 0.48, dashed=is_dash))
            painter.drawLine(
                QPointF(_px(lx), _px(leg_y + leg_h * 0.5)),
                QPointF(_px(lx + 4.5), _px(leg_y + leg_h * 0.5)),
            )
            _draw_text_box(
                painter, lx + 5.2, leg_y + 0.8, 12.8, 4.6, label_txt, pt=5.0, bold=True, color_hex=ink
            )
            lx += 17.6

        painter.restore()
        painter.setPen(_make_pen(ink, 0.30))
        painter.drawRect(QRectF(_px(chart_x), _px(chart_y), _px(chart_w), _px(chart_h)))

        # Link Summary Table below chart
        tbl_x = bar_x
        tbl_w = bar_w
        tbl_y = chart_y + chart_h + 11.5
        _draw_text_box(
            painter,
            tbl_x,
            tbl_y - 5.0,
            tbl_w,
            4.5,
            f"RF LINK SUMMARY TABLE ({len(profiles)} {'LINK' if len(profiles) == 1 else 'LINKS'})",
            pt=7.0,
            bold=True,
            color_hex=ink,
        )
        cols = [
            ("LINK", 0.30, "left"),
            ("FREQ", 0.095, "right"),
            (f"DIST ({dist_unit})", 0.085, "right"),
            ("FSPL (dB)", 0.095, "right"),
            (f"MIN CLR ({elev_unit})", 0.105, "right"),
            ("F1 %", 0.065, "right"),
            ("MAG AZ / MILS", 0.145, "right"),
            ("STATUS", 0.11, "center"),
        ]
        row_h = 6.8
        painter.fillRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(row_h)), QColor(ink))
        cx = tbl_x
        for hdr, frac, align in cols:
            cw = tbl_w * frac
            _draw_text_box(
                painter, cx + 1.0, tbl_y, cw - 2.0, row_h, hdr, pt=5.9, bold=True, color_hex="#FFFFFF", align=align
            )
            cx += cw

        cur_y = tbl_y + row_h
        usable_bottom = page_h - margin - 10.0
        link_col_w = tbl_w * cols[0][1] - 2.0
        link_font = _make_font(5.9, bold=True)
        link_fm = QFontMetricsF(link_font)
        for row_idx, pr in enumerate(profiles):
            if cur_y + row_h > usable_bottom:
                break
            if row_idx % 2 == 1:
                painter.fillRect(QRectF(_px(tbl_x), _px(cur_y), _px(tbl_w), _px(row_h)), QColor(stripe))
            r_status = str(pr.get("status") or "CLEAR").upper()
            r_col = status_colors.get(r_status, ink)
            raw_link_name = f"{pr.get('from_label', 'OBS1')} → {pr.get('to_label', 'OBS2')}"
            r_name = link_fm.elidedText(raw_link_name, Qt.ElideRight, _px(link_col_w))
            vals = [
                (r_name, True, False, ink),
                (f"{float(pr.get('freq_mhz', 915.0)):g} MHz", False, True, ink),
                (f"{float(pr.get('distance_km', 0.0)) * dist_factor:.2f}", False, True, ink),
                (f"{float(pr.get('fspl_db', 0.0)):.1f}", False, True, ink),
                (f"{float(pr.get('min_clearance_m', 0.0)) * elev_factor:+.1f}", True, True, ink),
                (f"{float(pr.get('min_fresnel_pct', 0.0)):.0f}%", False, True, ink),
                (
                    f"{float(pr.get('bearing_mag_deg', 0.0)):05.1f}° / {int(round(float(pr.get('bearing_mils', 0.0)))):04d}",
                    False,
                    True,
                    ink,
                ),
                (r_status, True, True, r_col),
            ]
            cx = tbl_x
            for (txt, is_b, is_m, col_h), (_, frac, align) in zip(vals, cols):
                cw = tbl_w * frac
                _draw_text_box(
                    painter, cx + 1.0, cur_y, cw - 2.0, row_h, txt, pt=5.9, bold=is_b, mono=is_m, color_hex=col_h, align=align
                )
                cx += cw
            painter.setPen(_make_pen(border, 0.15))
            painter.drawLine(
                QPointF(_px(tbl_x), _px(cur_y + row_h)),
                QPointF(_px(tbl_x + tbl_w), _px(cur_y + row_h)),
            )
            cur_y += row_h

        painter.setPen(_make_pen(ink, 0.25))
        painter.drawRect(QRectF(_px(tbl_x), _px(tbl_y), _px(tbl_w), _px(cur_y - tbl_y)))

        # Footer
        _draw_text_box(
            painter,
            margin + 4.0,
            page_h - margin - 6.0,
            inner_w - 8.0,
            5.0,
            f"GridForge RF / Line-of-Sight Link Card  |  k = 4/3 Effective Earth Radius  |  "
            f"WMM2025 Magnetic Bearings  |  EPSG:{layout.utm_epsg}",
            pt=6.1,
            align="center",
            color_hex=muted,
        )
    finally:
        painter.end()


def _set_exact_mediabox_mm(pdf_path: Path, page_width_mm: float, page_height_mm: float) -> None:
    """Set exact floating-point PDF MediaBox dimensions in points (Qt5 QPdfWriter rounds to int pts)."""
    from pypdf import PdfReader
    from pypdf.generic import RectangleObject

    w_pt = round(float(page_width_mm) * 72.0 / 25.4, 4)
    h_pt = round(float(page_height_mm) * 72.0 / 25.4, 4)
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    try:
        for page in reader.pages:
            page.mediabox = RectangleObject((0.0, 0.0, w_pt, h_pt))
            if "/CropBox" in page:
                page.cropbox = RectangleObject((0.0, 0.0, w_pt, h_pt))
            writer.add_page(page)
        tmp_path = pdf_path.with_suffix(".mb_tmp.pdf")
        with tmp_path.open("wb") as fh:
            writer.write(fh)
    finally:
        writer.close()
    tmp_path.replace(pdf_path)


def _worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GridForge supplemental vector PDF generator")
    parser.add_argument(
        "mode",
        choices=["calibration", "route-card", "elevation-profile", "atlas-index", "rf-link-card"],
    )
    parser.add_argument("spec_json", type=Path)
    parser.add_argument("output_pdf", type=Path)
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtGui import QGuiApplication

    qt_app = QGuiApplication([])
    try:
        spec = json.loads(args.spec_json.read_text(encoding="utf-8"))
        if args.mode == "calibration":
            _render_calibration_in_process(spec, args.output_pdf)
            _set_exact_mediabox_mm(args.output_pdf, spec["page_width_mm"], spec["page_height_mm"])
        elif args.mode == "route-card":
            _render_route_card_in_process(spec, args.output_pdf)
            _set_exact_mediabox_mm(
                args.output_pdf,
                spec["layout"]["page_width_mm"],
                spec["layout"]["page_height_mm"],
            )
        elif args.mode == "elevation-profile":
            _render_elevation_profile_in_process(spec, args.output_pdf)
            _set_exact_mediabox_mm(
                args.output_pdf,
                spec["layout"]["page_width_mm"],
                spec["layout"]["page_height_mm"],
            )
        elif args.mode == "atlas-index":
            _render_atlas_index_in_process(spec, args.output_pdf)
            _set_exact_mediabox_mm(args.output_pdf, spec["page_width_mm"], spec["page_height_mm"])
        elif args.mode == "rf-link-card":
            _render_rf_link_card_in_process(spec, args.output_pdf)
            _set_exact_mediabox_mm(
                args.output_pdf,
                spec["layout"]["page_width_mm"],
                spec["layout"]["page_height_mm"],
            )
    finally:
        qt_app.quit()
        del qt_app
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
