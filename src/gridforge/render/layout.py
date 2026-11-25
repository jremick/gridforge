"""Print layout and military topographic collar construction for PyQGIS 3.40.

Locks exact physical page dimensions (mm) and exact true-scale UTM map frame
extent so 1 km MGRS grid lines measure exact physical spacing with a protractor.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from gridforge.config import ColorMode, GridTick, JobWorkspace, SheetLayout
from gridforge.render.styles import DEFAULT_FONT_FAMILY, map_background_hex

# Per-scale metric & imperial scale bar parameters calibrated to fit cleanly in ~40 mm width.
# Format: (units_per_segment, segments_left, segments_right, unit_label, subdivision_count)
_METRIC_SCALEBAR_SPEC: dict[int, tuple[float, int, int, str, int]] = {
    15000: (200.0, 1, 2, "m", 2),   # 600 m = 40.00 mm
    24000: (250.0, 1, 3, "m", 2),   # 1000 m = 41.67 mm
    25000: (250.0, 1, 3, "m", 2),   # 1000 m = 40.00 mm
    48000: (500.0, 1, 3, "m", 2),   # 2000 m = 41.67 mm
    50000: (500.0, 1, 3, "m", 2),   # 2000 m = 40.00 mm
}

_IMPERIAL_SCALEBAR_SPEC: dict[int, tuple[float, int, int, str, int]] = {
    15000: (500.0, 1, 3, "ft", 2),   # 2000 ft = 40.64 mm
    24000: (1000.0, 1, 2, "ft", 2),  # 3000 ft = 38.10 mm
    25000: (1000.0, 1, 2, "ft", 2),  # 3000 ft = 36.58 mm
    48000: (2000.0, 1, 2, "ft", 2),  # 6000 ft = 38.10 mm
    50000: (2000.0, 1, 2, "ft", 2),  # 6000 ft = 36.58 mm
}


def _make_label_format(
    size_pt: float,
    color_hex: str = "#1A1A1A",
    *,
    bold: bool = False,
    italic: bool = False,
) -> Any:
    from qgis.core import QgsTextFormat, QgsUnitTypes
    from qgis.PyQt.QtGui import QColor, QFont

    font = QFont(DEFAULT_FONT_FAMILY)
    font.setBold(bold)
    font.setItalic(italic)

    fmt = QgsTextFormat()
    fmt.setFont(font)
    fmt.setSize(size_pt)
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    fmt.setColor(QColor(color_hex))
    return fmt


def _add_label(
    print_layout: Any,
    text: str,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    *,
    size_pt: float = 7.0,
    color_hex: str = "#1A1A1A",
    bold: bool = False,
    italic: bool = False,
    halign: str = "left",
    valign: str = "center",
    margin_x: float = 0.0,
    margin_y: float = 0.0,
) -> Any:
    from qgis.core import (
        QgsLayoutItemLabel,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt

    h_map = {
        "left": Qt.AlignLeft,
        "center": Qt.AlignHCenter,
        "right": Qt.AlignRight,
    }
    v_map = {
        "top": Qt.AlignTop,
        "center": Qt.AlignVCenter,
        "bottom": Qt.AlignBottom,
    }

    lbl = QgsLayoutItemLabel(print_layout)
    lbl.setText(text)
    lbl.setTextFormat(_make_label_format(size_pt, color_hex, bold=bold, italic=italic))
    lbl.setHAlign(h_map[halign])
    lbl.setVAlign(v_map[valign])
    lbl.setMarginX(margin_x)
    lbl.setMarginY(margin_y)
    lbl.attemptMove(QgsLayoutPoint(x_mm, y_mm, QgsUnitTypes.LayoutMillimeters))
    lbl.attemptResize(QgsLayoutSize(w_mm, h_mm, QgsUnitTypes.LayoutMillimeters))
    print_layout.addLayoutItem(lbl)
    return lbl


def _add_line(
    print_layout: Any,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
    *,
    color_hex: str = "#1A1A1A",
    width_mm: float = 0.25,
    dash_vector: list[float] | None = None,
) -> Any:
    from qgis.core import (
        QgsLayoutItemPolyline,
        QgsLineSymbol,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import QPointF, Qt
    from qgis.PyQt.QtGui import QColor, QPolygonF

    poly = QPolygonF([QPointF(x1_mm, y1_mm), QPointF(x2_mm, y2_mm)])
    item = QgsLayoutItemPolyline(poly, print_layout)

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)
    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), width_mm)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.FlatCap)
    if dash_vector:
        sl.setUseCustomDashPattern(True)
        sl.setCustomDashVector(dash_vector)
        sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sym.appendSymbolLayer(sl)
    item.setSymbol(sym)
    print_layout.addLayoutItem(item)
    return item


def _add_rect(
    print_layout: Any,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    *,
    fill_hex: str = "transparent",
    stroke_hex: str = "#1A1A1A",
    stroke_mm: float = 0.25,
) -> Any:
    from qgis.core import (
        QgsFillSymbol,
        QgsLayoutItemShape,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsUnitTypes,
    )

    rect = QgsLayoutItemShape(print_layout)
    rect.setShapeType(QgsLayoutItemShape.Rectangle)
    rect.attemptMove(QgsLayoutPoint(x_mm, y_mm, QgsUnitTypes.LayoutMillimeters))
    rect.attemptResize(QgsLayoutSize(w_mm, h_mm, QgsUnitTypes.LayoutMillimeters))

    props = {
        "color": fill_hex if fill_hex != "transparent" else "255,255,255,0",
        "style": "solid" if fill_hex != "transparent" else "no",
        "outline_color": stroke_hex,
        "outline_style": "solid" if stroke_mm > 0 else "no",
        "outline_width": f"{stroke_mm:.3f}",
        "outline_width_unit": "MM",
    }
    rect.setSymbol(QgsFillSymbol.createSimple(props))
    print_layout.addLayoutItem(rect)
    return rect


def _add_triangle(
    print_layout: Any,
    x_mm: float,
    y_mm: float,
    size_mm: float,
    *,
    fill_hex: str = "#111111",
) -> Any:
    from qgis.core import (
        QgsFillSymbol,
        QgsLayoutItemPolygon,
    )
    from qgis.PyQt.QtCore import QPointF
    from qgis.PyQt.QtGui import QPolygonF

    poly = QPolygonF(
        [
            QPointF(x_mm + size_mm / 2.0, y_mm),
            QPointF(x_mm + size_mm, y_mm + size_mm),
            QPointF(x_mm, y_mm + size_mm),
        ]
    )
    tri = QgsLayoutItemPolygon(poly, print_layout)
    tri.setSymbol(
        QgsFillSymbol.createSimple(
            {
                "color": fill_hex,
                "style": "solid",
                "outline_style": "no",
            }
        )
    )
    print_layout.addLayoutItem(tri)
    return tri


def _render_mgrs_ticks(
    print_layout: Any,
    layout: SheetLayout,
    ticks: list[GridTick],
    color_mode: ColorMode,
) -> None:
    """Draw exact 2.0 mm outward ticks and military principal-digit labels on all 4 edges."""
    tick_len = 2.0
    is_color = color_mode == ColorMode.COLOR
    tick_color = "#1B4F72" if is_color else "#000000"
    text_color = "#10324A" if is_color else "#000000"

    fx = layout.frame_x_mm
    fy = layout.frame_y_mm
    fw = layout.frame_width_mm
    fh = layout.frame_height_mm
    f_right = fx + fw
    f_bottom = fy + fh
    page_w = layout.page_width_mm

    for tick in ticks:
        stroke_w = 0.45 if tick.is_100km else 0.24

        if tick.edge == "top":
            tx = fx + tick.offset_mm
            _add_line(print_layout, tx, fy, tx, fy - tick_len, color_hex=tick_color, width_mm=stroke_w)
            _draw_horizontal_edge_label(
                print_layout,
                tick,
                center_x=tx,
                y_top=fy - tick_len - 4.0,
                height_mm=3.8,
                page_w=page_w,
                text_color=text_color,
                valign="bottom",
            )

        elif tick.edge == "bottom":
            tx = fx + tick.offset_mm
            _add_line(print_layout, tx, f_bottom, tx, f_bottom + tick_len, color_hex=tick_color, width_mm=stroke_w)
            _draw_horizontal_edge_label(
                print_layout,
                tick,
                center_x=tx,
                y_top=f_bottom + tick_len + 0.3,
                height_mm=3.9,
                page_w=page_w,
                text_color=text_color,
                valign="top",
            )

        elif tick.edge == "left":
            ty = fy + tick.offset_mm
            _add_line(print_layout, fx, ty, fx - tick_len, ty, color_hex=tick_color, width_mm=stroke_w)
            _draw_vertical_edge_label(
                print_layout,
                tick,
                edge="left",
                anchor_x=fx - tick_len - 0.5,
                center_y=ty,
                avail_w=max(6.5, fx - tick_len - 1.2),
                text_color=text_color,
            )

        elif tick.edge == "right":
            ty = fy + tick.offset_mm
            _add_line(print_layout, f_right, ty, f_right + tick_len, ty, color_hex=tick_color, width_mm=stroke_w)
            _draw_vertical_edge_label(
                print_layout,
                tick,
                edge="right",
                anchor_x=f_right + tick_len + 0.5,
                center_y=ty,
                avail_w=max(6.5, (page_w - f_right) - tick_len - 1.2),
                text_color=text_color,
            )


def _draw_horizontal_edge_label(
    print_layout: Any,
    tick: GridTick,
    *,
    center_x: float,
    y_top: float,
    height_mm: float,
    page_w: float,
    text_color: str,
    valign: str,
) -> None:
    """Render top/bottom edge Easting label with military prefix + bold principal digits."""
    if tick.is_first_on_edge:
        box_w = 18.0
        x_left = max(1.5, min(center_x - box_w / 2.0, page_w - box_w - 1.5))
        _add_label(
            print_layout,
            tick.full_label,
            x_left,
            y_top,
            box_w,
            height_mm,
            size_pt=6.0,
            color_hex=text_color,
            bold=True,
            halign="center",
            valign=valign,
        )
        return

    # Military style: smaller prefix digit(s) to the left of bold 7.5 pt principal digits
    prefix = tick.prefix_digits
    principal = tick.principal_digits
    if prefix:
        _add_label(
            print_layout,
            prefix,
            center_x - 6.2,
            y_top + (0.2 if valign == "top" else 0.0),
            4.2,
            height_mm - 0.3,
            size_pt=5.0,
            color_hex=text_color,
            bold=False,
            halign="right",
            valign=valign,
        )
        _add_label(
            print_layout,
            principal,
            center_x - 1.8,
            y_top,
            5.6,
            height_mm,
            size_pt=7.5,
            color_hex=text_color,
            bold=True,
            halign="left",
            valign=valign,
        )
    else:
        _add_label(
            print_layout,
            principal,
            center_x - 4.0,
            y_top,
            8.0,
            height_mm,
            size_pt=7.5,
            color_hex=text_color,
            bold=True,
            halign="center",
            valign=valign,
        )


def _draw_vertical_edge_label(
    print_layout: Any,
    tick: GridTick,
    *,
    edge: str,
    anchor_x: float,
    center_y: float,
    avail_w: float,
    text_color: str,
) -> None:
    """Render left/right edge Northing label cleanly inside the 12 mm side margin."""
    if tick.is_first_on_edge:
        # Stack cleanly onto 2 lines (e.g. "6264" / "000m N") so it never overflows the 12 mm side margin!
        km_str = f"{tick.prefix_digits}{tick.principal_digits}"
        suffix = "000m N"
        box_w = min(avail_w, 8.8)
        x_pos = anchor_x - box_w if edge == "left" else anchor_x
        align = "right" if edge == "left" else "left"
        _add_label(
            print_layout,
            km_str,
            x_pos,
            center_y - 2.6,
            box_w,
            2.9,
            size_pt=6.4,
            color_hex=text_color,
            bold=True,
            halign=align,
            valign="bottom",
        )
        _add_label(
            print_layout,
            suffix,
            x_pos,
            center_y + 0.1,
            box_w,
            2.4,
            size_pt=4.6,
            color_hex=text_color,
            bold=False,
            halign=align,
            valign="top",
        )
        return

    # Standard Northing tick: small prefix + bold 7.5 pt principal digits side by side (~5.8 mm wide)
    prefix = tick.prefix_digits
    principal = tick.principal_digits
    h_box = 4.0
    y_box = center_y - h_box / 2.0

    if edge == "left":
        # Right-aligned against tick: [prefix][principal] -> ends at anchor_x
        prin_w = 4.0
        pref_w = max(2.8, avail_w - prin_w)
        _add_label(
            print_layout,
            principal,
            anchor_x - prin_w,
            y_box,
            prin_w,
            h_box,
            size_pt=7.5,
            color_hex=text_color,
            bold=True,
            halign="right",
            valign="center",
        )
        if prefix:
            _add_label(
                print_layout,
                prefix,
                anchor_x - prin_w - pref_w,
                y_box - 0.15,
                pref_w,
                h_box,
                size_pt=5.0,
                color_hex=text_color,
                bold=False,
                halign="right",
                valign="center",
            )
    else:
        # Left-aligned from tick: [prefix][principal] -> starts at anchor_x
        pref_w = 2.8 if prefix else 0.0
        prin_w = max(4.0, avail_w - pref_w)
        if prefix:
            _add_label(
                print_layout,
                prefix,
                anchor_x,
                y_box - 0.15,
                pref_w,
                h_box,
                size_pt=5.0,
                color_hex=text_color,
                bold=False,
                halign="right",
                valign="center",
            )
        _add_label(
            print_layout,
            principal,
            anchor_x + pref_w + 0.2,
            y_box,
            prin_w,
            h_box,
            size_pt=7.5,
            color_hex=text_color,
            bold=True,
            halign="left",
            valign="center",
        )


def _add_scale_bar(
    print_layout: Any,
    map_item: Any,
    *,
    x_mm: float,
    y_mm: float,
    col_w_mm: float,
    scale: int,
    imperial: bool,
) -> Any:
    from qgis.core import (
        Qgis,
        QgsLayoutItemScaleBar,
        QgsLayoutPoint,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtGui import QColor

    spec_table = _IMPERIAL_SCALEBAR_SPEC if imperial else _METRIC_SCALEBAR_SPEC
    units_per_seg, seg_left, seg_right, unit_label, subdiv = spec_table.get(
        scale,
        (1000.0, 1, 2, "ft", 2) if imperial else (250.0, 1, 3, "m", 2),
    )

    bar = QgsLayoutItemScaleBar(print_layout)
    bar.setLinkedMap(map_item)
    bar.setStyle("Double Box")

    if imperial:
        dist_unit = getattr(Qgis.DistanceUnit, "Feet", QgsUnitTypes.DistanceFeet)
    else:
        dist_unit = getattr(Qgis.DistanceUnit, "Meters", QgsUnitTypes.DistanceMeters)

    bar.setUnits(dist_unit)
    bar.setUnitsPerSegment(units_per_seg)
    bar.setNumberOfSegmentsLeft(seg_left)
    bar.setNumberOfSegments(seg_right)
    if hasattr(bar, "setNumberOfSubdivisions"):
        bar.setNumberOfSubdivisions(subdiv)
    bar.setUnitLabel(unit_label)
    bar.setHeight(1.9)
    bar.setBoxContentSpace(0.5)
    bar.setLabelBarSpace(0.8)
    bar.setTextFormat(_make_label_format(5.4, "#1A1A1A", bold=False))
    bar.setFillColor(QColor("#1A1A1A"))
    bar.setFillColor2(QColor("#FFFFFF"))
    bar.setLineColor(QColor("#1A1A1A"))
    bar.setLineWidth(0.22)
    print_layout.addLayoutItem(bar)
    bar.update()

    # Center scalebar horizontally within the column
    bar_w = bar.sizeWithUnits().width()
    if bar_w <= 0 or bar_w > col_w_mm:
        # Compute analytic fallback width for centering
        meters_per_unit = 0.3048 if imperial else 1.0
        total_units = (seg_left + seg_right) * units_per_seg
        bar_w = min(col_w_mm, total_units * meters_per_unit / scale * 1000.0 + 7.0)
    x_centered = x_mm + max(0.0, (col_w_mm - bar_w) / 2.0)

    bar.attemptMove(QgsLayoutPoint(x_centered, y_mm, QgsUnitTypes.LayoutMillimeters))
    return bar


def _add_svg_picture(
    print_layout: Any,
    svg_path: Path,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
) -> Any:
    from qgis.core import (
        QgsLayoutItemPicture,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsUnitTypes,
    )

    pic = QgsLayoutItemPicture(print_layout)
    if hasattr(QgsLayoutItemPicture, "FormatSVG"):
        pic.setMode(QgsLayoutItemPicture.FormatSVG)
    pic.setPicturePath(str(svg_path))
    pic.setResizeMode(QgsLayoutItemPicture.Zoom)
    pic.setPictureAnchor(QgsLayoutItemPicture.Middle)
    pic.attemptMove(QgsLayoutPoint(x_mm, y_mm, QgsUnitTypes.LayoutMillimeters))
    pic.attemptResize(QgsLayoutSize(w_mm, h_mm, QgsUnitTypes.LayoutMillimeters))
    print_layout.addLayoutItem(pic)
    return pic


def _render_compact_map_key(
    print_layout: Any,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    *,
    color_mode: ColorMode,
    date_str: str,
    workspace: JobWorkspace | None = None,
) -> None:
    """Draw a deterministic, non-overlapping vector cartographic key legend in Col 4."""
    is_color = color_mode == ColorMode.COLOR
    regional = (workspace.regional_overlays or {}) if workspace is not None else {}
    carto = workspace.layout.spec.carto if workspace is not None else None

    # Outer box frame
    _add_rect(print_layout, x_mm, y_mm, w_mm, h_mm, fill_hex="#FFFFFF", stroke_hex="#333333", stroke_mm=0.20)

    # Box header
    _add_label(
        print_layout,
        "MAP KEY",
        x_mm + 1.2,
        y_mm + 0.4,
        w_mm - 2.4,
        2.8,
        size_pt=5.6,
        color_hex="#111111",
        bold=True,
        halign="center",
        valign="center",
    )
    _add_line(print_layout, x_mm + 1.0, y_mm + 3.4, x_mm + w_mm - 1.0, y_mm + 3.4, color_hex="#888888", width_mm=0.14)

    sx1 = x_mm + 1.5
    sx2 = x_mm + 7.6
    lbl_x = x_mm + 8.7
    lbl_w = max(10.0, w_mm - 9.6)

    has_fema = "fema" in regional
    has_towers = "towers" in regional
    has_au_parks = ("au-parks" in regional) or ("au_parks" in regional)
    has_au_hydro = ("au-hydro" in regional) or ("au_hydro" in regional)
    has_au_aviation = ("au-aviation" in regional) or ("au_aviation" in regional)
    has_au_state_trails = any(
        k in regional for k in ("au-nsw-trails", "au_nsw_trails", "au-vic-trails", "au_vic_trails")
    )
    has_slope = bool(
        workspace is not None
        and workspace.slope_tif is not None
        and workspace.slope_tif.is_file()
        and carto is not None
        and carto.show_slope_shading
    )
    has_waypoints = bool(
        workspace is not None
        and workspace.waypoints_gpkg is not None
        and workspace.waypoints_gpkg.is_file()
    )
    has_infra = bool(carto is None or carto.show_power or carto.show_pipelines)
    has_us_row = has_fema or has_towers
    has_au_row = (has_au_parks or has_au_hydro or has_au_aviation) and not has_us_row

    # Keep total rows cleanly readable (<= 11 rows inside 31.8 mm box height)
    dynamic_count = (
        int(has_us_row or has_au_row)
        + int(has_au_state_trails)
        + int(has_slope)
        + int(has_waypoints)
    )
    if 8 + int(has_infra) + dynamic_count > 11:
        has_infra = False
    if 8 + int(has_infra) + dynamic_count > 11 and has_au_row and has_au_state_trails:
        has_au_row = False

    extra_rows = (
        int(has_infra)
        + int(has_us_row or has_au_row)
        + int(has_au_state_trails)
        + int(has_slope)
        + int(has_waypoints)
    )
    total_rows = 8 + extra_rows
    meta_y = y_mm + h_mm - 4.7
    meta_divider_y = meta_y - 0.35
    row_y0 = y_mm + 4.55
    avail_h = max(8.0, (meta_divider_y - 1.50) - row_y0)
    pitch = min(2.75, avail_h / max(1, total_rows - 1)) if total_rows > 1 else 2.75
    if total_rows <= 8:
        lbl_pt = 4.9
    elif total_rows <= 10:
        lbl_pt = 4.4
    else:
        lbl_pt = 4.1

    def _ry(idx: int) -> float:
        return row_y0 + idx * pitch

    idx = 0
    # Row: Major Highway / Road
    ry = _ry(idx); idx += 1
    _add_line(print_layout, sx1, ry, sx2, ry, color_hex="#5E1D12" if is_color else "#000000", width_mm=1.0)
    _add_line(print_layout, sx1 + 0.15, ry, sx2 - 0.15, ry, color_hex="#D97925" if is_color else "#C8C8C8", width_mm=0.60)
    _add_label(print_layout, "Major Road", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Secondary / Minor Road
    ry = _ry(idx); idx += 1
    _add_line(print_layout, sx1, ry, sx2, ry, color_hex="#4A4A4A" if is_color else "#222222", width_mm=0.68)
    _add_line(print_layout, sx1 + 0.15, ry, sx2 - 0.15, ry, color_hex="#FFFFFF", width_mm=0.38)
    _add_label(print_layout, "Minor Road", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Track / Trail
    ry = _ry(idx); idx += 1
    _add_line(
        print_layout,
        sx1,
        ry,
        sx2,
        ry,
        color_hex="#5E442B" if is_color else "#1A1A1A",
        width_mm=0.28,
        dash_vector=[1.4, 0.8],
    )
    _add_label(print_layout, "Track / Trail", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Railway (line + tiny ticks)
    ry = _ry(idx); idx += 1
    _add_line(print_layout, sx1, ry, sx2, ry, color_hex="#1A1A1A", width_mm=0.26)
    for tx in (sx1 + 1.3, sx1 + 3.0, sx1 + 4.7):
        _add_line(print_layout, tx, ry - 0.55, tx, ry + 0.55, color_hex="#1A1A1A", width_mm=0.20)
    _add_label(print_layout, "Railway", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Water / Stream
    ry = _ry(idx); idx += 1
    if is_color:
        _add_rect(print_layout, sx1, ry - 0.7, 2.4, 1.4, fill_hex="#A6D1EC", stroke_hex="#3A82B5", stroke_mm=0.15)
        _add_line(print_layout, sx1 + 3.0, ry, sx2, ry, color_hex="#2B79B3", width_mm=0.35)
    else:
        _add_rect(print_layout, sx1, ry - 0.7, 2.4, 1.4, fill_hex="#FFFFFF", stroke_hex="#1A1A1A", stroke_mm=0.18)
        _add_line(print_layout, sx1 + 0.2, ry, sx1 + 2.2, ry, color_hex="#666666", width_mm=0.12)
        _add_line(print_layout, sx1 + 3.0, ry, sx2, ry, color_hex="#1A1A1A", width_mm=0.35)
    _add_label(print_layout, "Water / Stream", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Index / Minor Contour
    ry = _ry(idx); idx += 1
    _add_line(print_layout, sx1, ry - 0.3, sx2, ry - 0.3, color_hex="#8F5B28" if is_color else "#1A1A1A", width_mm=0.30)
    _add_line(print_layout, sx1, ry + 0.4, sx2, ry + 0.4, color_hex="#A87846" if is_color else "#555555", width_mm=0.14)
    _add_label(print_layout, "Index / Contour", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: MGRS 1 km Grid
    ry = _ry(idx); idx += 1
    _add_line(print_layout, sx1, ry, sx2, ry, color_hex="#1B4F72" if is_color else "#000000", width_mm=0.20)
    _add_label(print_layout, "MGRS 1 km Grid", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Peak & Building
    ry = _ry(idx); idx += 1
    _add_triangle(print_layout, sx1 + 0.3, ry - 0.8, 1.6, fill_hex="#111111")
    _add_rect(
        print_layout,
        sx1 + 3.4,
        ry - 0.65,
        1.6,
        1.3,
        fill_hex="#6E6E6E" if is_color else "#262626",
        stroke_hex="#222222",
        stroke_mm=0.10,
    )
    _add_label(print_layout, "Peak / Building", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Power / Pipeline (when active)
    if has_infra:
        ry = _ry(idx); idx += 1
        _add_line(print_layout, sx1, ry, sx2, ry, color_hex="#555555" if is_color else "#222222", width_mm=0.18)
        for tx in (sx1 + 1.8, sx1 + 4.2):
            _add_line(print_layout, tx, ry - 0.5, tx, ry + 0.5, color_hex="#555555" if is_color else "#222222", width_mm=0.18)
        _add_label(print_layout, "Power / Pipe", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Active US Regional Overlay (FEMA Flood / FAA Towers)
    if has_us_row:
        ry = _ry(idx); idx += 1
        if has_fema:
            _add_rect(
                print_layout,
                sx1,
                ry - 0.65,
                2.5,
                1.3,
                fill_hex="#DCEEFF" if is_color else "#FFFFFF",
                stroke_hex="#1565C0" if is_color else "#333333",
                stroke_mm=0.18,
            )
            _add_line(
                print_layout,
                sx1 + 0.3,
                ry + 0.5,
                sx1 + 2.2,
                ry - 0.5,
                color_hex="#1976D2" if is_color else "#444444",
                width_mm=0.14,
            )
        if has_towers:
            tx = sx1 + (4.4 if has_fema else 3.0)
            _add_line(print_layout, tx, ry - 0.75, tx - 0.6, ry + 0.65, color_hex="#1B4F72" if is_color else "#000000", width_mm=0.22)
            _add_line(print_layout, tx, ry - 0.75, tx + 0.6, ry + 0.65, color_hex="#1B4F72" if is_color else "#000000", width_mm=0.22)
        tag = "Flood / Tower" if (has_fema and has_towers) else ("FEMA Flood" if has_fema else "FAA Obstacle")
        _add_label(print_layout, tag, lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Active AU Regional Overlays (CAPAD Parks/IPA, Outback Bore/Water, Airfield/Homestead)
    if has_au_row:
        ry = _ry(idx); idx += 1
        if has_au_parks:
            _add_line(
                print_layout,
                sx1,
                ry,
                sx1 + (3.2 if (has_au_hydro or has_au_aviation) else 6.0),
                ry,
                color_hex="#2B7A3E" if is_color else "#1A1A1A",
                width_mm=0.40,
                dash_vector=[1.4, 0.7],
            )
        if has_au_hydro or has_au_aviation:
            bx = sx1 + (4.2 if has_au_parks else 1.8)
            _add_rect(
                print_layout,
                bx,
                ry - 0.6,
                1.2,
                1.2,
                fill_hex="#1B6CA8" if (is_color and has_au_hydro) else ("#1B4F72" if is_color else "#1A1A1A"),
                stroke_hex="#1A1A1A",
                stroke_mm=0.14,
            )
        if has_au_parks and (has_au_hydro or has_au_aviation):
            au_tag = "Park / Bore-ALA" if has_au_aviation else "Park / Waterpt"
        elif has_au_parks:
            au_tag = "Nat. Park / IPA"
        elif has_au_aviation:
            au_tag = "Airfield / Stn"
        else:
            au_tag = "Bore / Waterpt"
        _add_label(print_layout, au_tag, lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: NSW / VIC State Fire Trails, Huts & Trigs
    if has_au_state_trails:
        ry = _ry(idx); idx += 1
        _add_line(
            print_layout,
            sx1,
            ry,
            sx1 + 3.8,
            ry,
            color_hex="#D95F02" if is_color else "#000000",
            width_mm=0.42,
            dash_vector=[1.4, 0.65] if is_color else [1.5, 0.55, 0.5, 0.55],
        )
        _add_triangle(print_layout, sx1 + 4.5, ry - 0.68, 1.35, fill_hex="#7A3E1D" if is_color else "#111111")
        _add_label(print_layout, "Fire Trl / Hut / Trig", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Slope-Angle Shading (Avalanche >30 deg)
    if has_slope:
        ry = _ry(idx); idx += 1
        swatches = (
            ("#F6E05E", "#ED8936", "#E53E3E", "#805AD5")
            if is_color
            else ("#C0C0C0", "#888888", "#484848", "#181818")
        )
        sw_w = (sx2 - sx1) / len(swatches)
        for s_idx, sw_hex in enumerate(swatches):
            _add_rect(
                print_layout,
                sx1 + s_idx * sw_w,
                ry - 0.58,
                sw_w,
                1.16,
                fill_hex=sw_hex,
                stroke_hex="#444444",
                stroke_mm=0.08,
            )
        _add_label(print_layout, "Slope 30/35/45°+", lbl_x, ry - 1.15, lbl_w, 2.3, size_pt=lbl_pt, valign="center")

    # Row: Custom Tactical Waypoints & Route Legs
    if has_waypoints:
        ry = _ry(idx); idx += 1
        wp_c = "#C2185B" if is_color else "#111111"
        _add_line(
            print_layout,
            sx1,
            ry,
            sx1 + 4.2,
            ry,
            color_hex=wp_c,
            width_mm=0.40,
            dash_vector=[1.5, 0.7],
        )
        _add_rect(
            print_layout,
            sx1 + 4.8,
            ry - 0.55,
            1.10,
            1.10,
            fill_hex=wp_c,
            stroke_hex="#111111",
            stroke_mm=0.14,
        )
        _add_label(print_layout, "Waypoints / Route", lbl_x, ry - 1.05, lbl_w, 2.1, size_pt=lbl_pt, valign="center")

    # Sheet Metadata footer inside Key Box
    _add_line(print_layout, x_mm + 1.0, meta_divider_y, x_mm + w_mm - 1.0, meta_divider_y, color_hex="#BBBBBB", width_mm=0.14)
    mode_tag = "COLOR" if is_color else "B&W PRINT"
    _add_label(
        print_layout,
        f"DATE: {date_str} | {mode_tag}",
        x_mm + 1.0,
        meta_y,
        w_mm - 2.0,
        2.15,
        size_pt=4.5,
        color_hex="#333333",
        halign="center",
        valign="center",
    )
    _add_label(
        print_layout,
        "GRIDFORGE TOPOGRAPHIC SHEET",
        x_mm + 1.0,
        meta_y + 2.1,
        w_mm - 2.0,
        2.1,
        size_pt=4.3,
        color_hex="#555555",
        bold=True,
        halign="center",
        valign="center",
    )


def _format_attribution_footer(attributions: list[str]) -> str:
    """Guarantee mandatory ODbL, Copernicus DEM, and GridForge notices in footer."""
    ordered: list[str] = []
    seen: set[str] = set()
    for item in attributions:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            ordered.append(cleaned)
            seen.add(cleaned)

    joined = " | ".join(ordered)
    if "GridForge" not in joined:
        ordered.append("Rendered by GridForge (GPL-3.0)")
    return " | ".join(ordered)


def _split_base_and_vector_layers(ordered_layers: list[Any]) -> tuple[list[Any], list[Any]]:
    """Split top-to-bottom layers into (top_vector_layers, bottom_blended_layers).

    When a map item contains a raster layer or non-normal blend mode (such as
    Multiply hillshade or semi-transparent slope-angle shading), placing all
    bottom raster/landcover layers in a dedicated bottom ``QgsLayoutItemMap``
    and all vector layers above them in a second transparent ``QgsLayoutItemMap``
    preserves shaded relief and avalanche slope tinting while keeping contours,
    fire trails, roads, water, labels, waypoints, and the MGRS 1 km grid as
    100% pure crisp PDF vectors.
    """
    from qgis.core import QgsRasterLayer
    from qgis.PyQt.QtGui import QPainter

    split_idx: int | None = None
    for idx, lyr in enumerate(ordered_layers):
        is_raster = isinstance(lyr, QgsRasterLayer)
        has_non_normal_blend = (
            hasattr(lyr, "blendMode") and lyr.blendMode() != QPainter.CompositionMode_SourceOver
        )
        if is_raster or has_non_normal_blend:
            split_idx = idx
            break

    if split_idx is None:
        return ordered_layers, []
    return ordered_layers[:split_idx], ordered_layers[split_idx:]


def _configure_locked_map_item(
    print_layout: Any,
    layout: SheetLayout,
    layers: list[Any],
    *,
    background_hex: str | None,
    frame_enabled: bool,
) -> Any:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsLayoutItemMap,
        QgsLayoutMeasurement,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsRectangle,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtGui import QColor

    map_item = QgsLayoutItemMap(print_layout)
    map_item.attemptMove(
        QgsLayoutPoint(layout.frame_x_mm, layout.frame_y_mm, QgsUnitTypes.LayoutMillimeters)
    )
    map_item.attemptResize(
        QgsLayoutSize(layout.frame_width_mm, layout.frame_height_mm, QgsUnitTypes.LayoutMillimeters)
    )

    utm_crs = QgsCoordinateReferenceSystem(f"EPSG:{layout.utm_epsg}")
    map_item.setCrs(utm_crs)
    map_item.setLayers(layers)
    map_item.setKeepLayerSet(True)

    utm_bbox = layout.utm_bbox
    map_item.setExtent(QgsRectangle(utm_bbox.min_x, utm_bbox.min_y, utm_bbox.max_x, utm_bbox.max_y))

    target_scale = float(layout.spec.scale)
    if abs(map_item.scale() - target_scale) / target_scale > 1e-5:
        map_item.setScale(target_scale)

    if background_hex is not None:
        map_item.setBackgroundEnabled(True)
        map_item.setBackgroundColor(QColor(background_hex))
    else:
        map_item.setBackgroundEnabled(False)

    map_item.setFrameEnabled(frame_enabled)
    if frame_enabled:
        map_item.setFrameStrokeWidth(QgsLayoutMeasurement(0.42, QgsUnitTypes.LayoutMillimeters))
        map_item.setFrameStrokeColor(QColor("#1A1A1A"))

    print_layout.addLayoutItem(map_item)
    return map_item


def build_print_layout(
    project: Any,
    workspace: JobWorkspace,
    ordered_layers: list[Any],
) -> tuple[Any, Any]:
    """Build a locked true-scale ``QgsPrintLayout`` with full military collar.

    Returns ``(print_layout, map_item)``.
    """
    from qgis.core import (
        QgsLayoutSize,
        QgsPrintLayout,
        QgsUnitTypes,
    )

    layout = workspace.layout
    spec = layout.spec
    mgrs_meta = workspace.mgrs_meta

    print_layout = QgsPrintLayout(project)
    print_layout.initializeDefaults()
    print_layout.setName("GridForge Sheet")

    # 1. Exact physical page size in millimeters
    page = print_layout.pageCollection().page(0)
    page.setPageSize(
        QgsLayoutSize(
            layout.page_width_mm,
            layout.page_height_mm,
            QgsUnitTypes.LayoutMillimeters,
        )
    )

    # 2. Dual-stack true-scale UTM map frames:
    #    - Bottom map: background + landcover + Multiply hillshade (clean 300 DPI shaded relief)
    #    - Top map: transparent pure-vector stack (water, contours, roads, points, MGRS grid, overlays)
    top_vector_layers, bottom_blended_layers = _split_base_and_vector_layers(ordered_layers)
    bg_hex = map_background_hex(spec.color_mode)

    if bottom_blended_layers and top_vector_layers:
        _configure_locked_map_item(
            print_layout,
            layout,
            bottom_blended_layers,
            background_hex=bg_hex,
            frame_enabled=False,
        )
        map_item = _configure_locked_map_item(
            print_layout,
            layout,
            top_vector_layers,
            background_hex=None,
            frame_enabled=True,
        )
    else:
        map_item = _configure_locked_map_item(
            print_layout,
            layout,
            ordered_layers,
            background_hex=bg_hex,
            frame_enabled=True,
        )

    # Crisp double neatline outer border (0.8 mm outside map frame)
    _add_rect(
        print_layout,
        layout.frame_x_mm - 0.8,
        layout.frame_y_mm - 0.8,
        layout.frame_width_mm + 1.6,
        layout.frame_height_mm + 1.6,
        fill_hex="transparent",
        stroke_hex="#1A1A1A",
        stroke_mm=0.20,
    )

    # 3. Top Header Band (y = 1.4 .. 7.6 mm, strictly above top tick labels)
    fx = layout.frame_x_mm
    fw = layout.frame_width_mm
    title_text = (spec.title.strip() if spec.title else f"MGRS {mgrs_meta.center_mgrs_10m}").upper()
    squares_str = "/".join(mgrs_meta.square_ids_100km) if mgrs_meta.square_ids_100km else ""
    right_header = (
        f"MGRS {layout.gzd} {squares_str} | UTM {layout.utm_zone}{layout.utm_hemisphere} (EPSG:{layout.utm_epsg})"
    )

    left_w = fw * 0.40
    center_w = fw * 0.22
    right_w = fw - left_w - center_w

    _add_label(
        print_layout,
        title_text,
        fx,
        1.4,
        left_w,
        6.2,
        size_pt=8.8 if len(title_text) <= 28 else 7.4,
        color_hex="#111111",
        bold=True,
        halign="left",
        valign="center",
    )
    _add_label(
        print_layout,
        f"SCALE 1:{spec.scale:,}",
        fx + left_w,
        1.4,
        center_w,
        6.2,
        size_pt=8.5,
        color_hex="#111111",
        bold=True,
        halign="center",
        valign="center",
    )
    _add_label(
        print_layout,
        right_header,
        fx + left_w + center_w,
        1.4,
        right_w,
        6.2,
        size_pt=7.0,
        color_hex="#1A1A1A",
        bold=False,
        halign="right",
        valign="center",
    )

    # 4. MGRS 1 km Collar Ticks & Military Principal-Digit Labels on all 4 edges
    if spec.carto.show_grid:
        _render_mgrs_ticks(print_layout, layout, mgrs_meta.ticks, spec.color_mode)

    # 5. Bottom Collar Band (non-overlapping 4-column layout adapting cleanly to A4/Letter/Legal/A3)
    f_bottom = layout.frame_y_mm + layout.frame_height_mm
    divider_y = f_bottom + 6.7
    _add_line(print_layout, fx, divider_y, fx + fw, divider_y, color_hex="#222222", width_mm=0.30)

    collar_y = f_bottom + 7.6
    collar_h = 31.8  # ends at f_bottom + 39.4 mm
    gap = 2.6
    usable_w = fw - 3.0 * gap

    w_col1 = usable_w * 0.255  # Declination Diagram SVG
    w_col2 = usable_w * 0.295  # Metric + Imperial Scale Bars & Datum/Contours
    w_col3 = usable_w * 0.235  # MGRS Grid Reference Box SVG
    w_col4 = usable_w - w_col1 - w_col2 - w_col3  # Compact Map Key + Metadata

    x_col1 = fx
    x_col2 = x_col1 + w_col1 + gap
    x_col3 = x_col2 + w_col2 + gap
    x_col4 = x_col3 + w_col3 + gap

    # Col 1: Declination Diagram SVG
    if workspace.declination_svg.is_file():
        _add_svg_picture(print_layout, workspace.declination_svg, x_col1, collar_y, w_col1, collar_h)
    else:
        _add_rect(print_layout, x_col1, collar_y, w_col1, collar_h, stroke_hex="#888888", stroke_mm=0.2)

    # Col 2: Scale Title + Metric Scale Bar + Imperial Scale Bar + Contour/Datum Info
    _add_label(
        print_layout,
        f"SCALE 1:{spec.scale:,}",
        x_col2,
        collar_y + 0.2,
        w_col2,
        3.2,
        size_pt=7.2,
        color_hex="#111111",
        bold=True,
        halign="center",
        valign="center",
    )
    _add_scale_bar(
        print_layout,
        map_item,
        x_mm=x_col2,
        y_mm=collar_y + 3.8,
        col_w_mm=w_col2,
        scale=spec.scale,
        imperial=False,
    )
    _add_scale_bar(
        print_layout,
        map_item,
        x_mm=x_col2,
        y_mm=collar_y + 13.0,
        col_w_mm=w_col2,
        scale=spec.scale,
        imperial=True,
    )

    _add_label(
        print_layout,
        f"CONTOUR INTERVAL: {workspace.contour_interval_display}",
        x_col2,
        collar_y + 22.8,
        w_col2,
        3.0,
        size_pt=5.8,
        color_hex="#111111",
        bold=True,
        halign="center",
        valign="center",
    )
    v_datum = (workspace.vertical_datum or "EGM2008").strip()
    _add_label(
        print_layout,
        f"DATUM: WGS84 / VERTICAL: {v_datum}",
        x_col2,
        collar_y + 25.8,
        w_col2,
        2.8,
        size_pt=5.4,
        color_hex="#222222",
        halign="center",
        valign="center",
    )
    _add_label(
        print_layout,
        f"UTM ZONE {layout.utm_zone}{layout.utm_hemisphere} (EPSG:{layout.utm_epsg}) | 1 km MGRS GRID",
        x_col2,
        collar_y + 28.6,
        w_col2,
        2.8,
        size_pt=5.2,
        color_hex="#333333",
        halign="center",
        valign="center",
    )

    # Col 3: MGRS Grid Reference Box SVG
    if workspace.grid_ref_svg.is_file():
        _add_svg_picture(print_layout, workspace.grid_ref_svg, x_col3, collar_y, w_col3, collar_h)
    else:
        _add_rect(print_layout, x_col3, collar_y, w_col3, collar_h, stroke_hex="#888888", stroke_mm=0.2)

    # Col 4: Compact Cartographic Key Legend + Sheet Info
    date_str = spec.date_iso or date.today().isoformat()
    _render_compact_map_key(
        print_layout,
        x_col4,
        collar_y,
        w_col4,
        collar_h,
        color_mode=spec.color_mode,
        date_str=date_str,
        workspace=workspace,
    )

    # 6. Bottom-most Mandatory Attribution Footer Band (adaptive sizing for US overlays)
    footer_rule_y = f_bottom + 40.0
    _add_line(print_layout, fx, footer_rule_y, fx + fw, footer_rule_y, color_hex="#999999", width_mm=0.15)
    footer_text = _format_attribution_footer(workspace.attributions)
    if len(footer_text) <= 220:
        footer_pt = 5.0
    elif len(footer_text) <= 360:
        footer_pt = 4.4
    else:
        footer_pt = 4.0
    _add_label(
        print_layout,
        footer_text,
        fx,
        footer_rule_y + 0.35,
        fw,
        max(6.2, layout.page_height_mm - footer_rule_y - 1.1),
        size_pt=footer_pt,
        color_hex="#333333",
        halign="left",
        valign="top",
    )

    return print_layout, map_item


def export_layout_to_pdf(
    print_layout: Any,
    output_pdf: Path,
    *,
    dpi: int = 300,
    geopdf: bool = False,
) -> Path:
    """Export ``QgsPrintLayout`` to a crisp vector+hillshade print-ready PDF."""
    from qgis.core import Qgis, QgsLayoutExporter, QgsRenderContext

    output_pdf = Path(output_pdf).resolve()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    exporter = QgsLayoutExporter(print_layout)
    settings = QgsLayoutExporter.PdfExportSettings()
    settings.dpi = float(dpi)
    settings.rasterizeWholeImage = False
    settings.forceVectorOutput = False
    settings.writeGeoPdf = bool(geopdf)
    settings.exportMetadata = True
    settings.simplifyGeometries = False

    try:
        settings.textRenderFormat = Qgis.TextRenderFormat.AlwaysText
    except AttributeError:
        settings.textRenderFormat = QgsRenderContext.TextFormatAlwaysText

    status = exporter.exportToPdf(str(output_pdf), settings)
    if status != QgsLayoutExporter.Success:
        raise RuntimeError(f"QgsLayoutExporter.exportToPdf failed with status code {status}")

    if not output_pdf.is_file() or output_pdf.stat().st_size == 0:
        raise RuntimeError(f"PDF export reported success but output file is missing/empty: {output_pdf}")

    return output_pdf
