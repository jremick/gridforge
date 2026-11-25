"""Cartographic styling for GridForge (dedicated Color and B&W print themes).

Supports both versioned QGIS 3.40 ``.qml`` files under ``assets/styles/{color,bw}/``
and programmatic PyQGIS 3.40 builders with guaranteed fallback so every rule-based
renderer, symbol level, raster Multiply blend, and PAL labeling configuration
loads cleanly without silent failures.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gridforge.config import ColorMode, Units

logger = logging.getLogger(__name__)

# Ordered roles matching the exact rendering pipeline.
STYLE_ROLES: tuple[str, ...] = (
    "landcover",
    "hillshade",
    "water_polygons",
    "contours",
    "waterways",
    "buildings",
    "transport",
    "points",
    "mgrs_grid_1km",
    "mgrs_grid_100km",
    "mgrs_zone_boundary",
    "mgrs_square_labels",
    "overlay_polygons",
    "overlay_lines",
    "overlay_points",
)

DEFAULT_FONT_FAMILY = "Liberation Sans"


def get_styles_root() -> Path:
    """Locate ``assets/styles`` relative to the repository or installed package."""
    candidates = [
        Path(__file__).resolve().parents[3] / "assets" / "styles",
        Path.cwd() / "assets" / "styles",
        Path("/app/assets/styles"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def get_qml_path(role: str, color_mode: ColorMode, styles_root: Path | None = None) -> Path:
    """Return the versioned ``.qml`` path for a layer role and theme."""
    root = styles_root if styles_root is not None else get_styles_root()
    return root / color_mode.value / f"{role}.qml"


def map_background_hex(color_mode: ColorMode) -> str:
    """Return the map frame canvas background color hex for the chosen theme."""
    return "#FAF9F5" if color_mode == ColorMode.COLOR else "#FFFFFF"


def _make_text_format(
    size_pt: float,
    color_hex: str,
    *,
    bold: bool = False,
    italic: bool = False,
    halo_hex: str = "#FFFFFF",
    halo_mm: float = 0.65,
    halo_opacity: float = 0.92,
    opacity: float = 1.0,
    font_family: str = DEFAULT_FONT_FAMILY,
) -> Any:
    """Construct a PyQGIS ``QgsTextFormat`` with crisp vector typography and halo."""
    from qgis.core import QgsTextBufferSettings, QgsTextFormat, QgsUnitTypes
    from qgis.PyQt.QtGui import QColor, QFont

    font = QFont(font_family)
    font.setBold(bold)
    font.setItalic(italic)

    fmt = QgsTextFormat()
    fmt.setFont(font)
    fmt.setSize(size_pt)
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    fmt.setColor(QColor(color_hex))
    fmt.setOpacity(opacity)

    if halo_mm > 0:
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(halo_mm)
        buf.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        buf.setColor(QColor(halo_hex))
        buf.setOpacity(halo_opacity)
        fmt.setBuffer(buf)

    return fmt


def _simple_fill_symbol(
    fill_hex: str,
    stroke_hex: str = "#000000",
    stroke_mm: float = 0.0,
    *,
    stroke_style: str = "no",
    fill_opacity: float = 1.0,
) -> Any:
    from qgis.core import QgsFillSymbol

    is_transparent = fill_hex == "transparent"
    props: dict[str, str] = {
        "color": "0,0,0,0" if is_transparent else fill_hex,
        "style": "no" if is_transparent else "solid",
        "outline_color": stroke_hex,
        "outline_style": stroke_style if stroke_mm > 0 else "no",
        "outline_width": f"{stroke_mm:.3f}",
        "outline_width_unit": "MM",
    }
    sym = QgsFillSymbol.createSimple(props)
    if fill_opacity < 1.0:
        sym.setOpacity(fill_opacity)
    return sym


def _hatch_fill_symbol(
    line_hex: str,
    angle_deg: float,
    spacing_mm: float,
    line_mm: float = 0.12,
    *,
    bg_hex: str | None = None,
    outline_hex: str | None = None,
    outline_mm: float = 0.0,
    dash_vector: list[float] | None = None,
) -> Any:
    """Build a crisp vector line-pattern hatch fill (ideal for B&W laser prints)."""
    from qgis.core import (
        QgsFillSymbol,
        QgsLinePatternFillSymbolLayer,
        QgsLineSymbol,
        QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsFillSymbol()
    # Remove default symbol layer
    sym.deleteSymbolLayer(0)

    if bg_hex is not None:
        bg = QgsSimpleFillSymbolLayer(QColor(bg_hex))
        bg.setStrokeStyle(Qt.NoPen)
        sym.appendSymbolLayer(bg)

    pattern = QgsLinePatternFillSymbolLayer()
    pattern.setLineAngle(angle_deg)
    pattern.setDistance(spacing_mm)
    pattern.setDistanceUnit(QgsUnitTypes.RenderMillimeters)

    sub_line = QgsLineSymbol()
    sub_line.deleteSymbolLayer(0)
    sl = QgsSimpleLineSymbolLayer(QColor(line_hex), line_mm)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    if dash_vector:
        sl.setUseCustomDashPattern(True)
        sl.setCustomDashVector(dash_vector)
        sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sub_line.appendSymbolLayer(sl)
    pattern.setSubSymbol(sub_line)
    sym.appendSymbolLayer(pattern)

    if outline_hex is not None and outline_mm > 0:
        outline = QgsSimpleFillSymbolLayer()
        outline.setBrushStyle(Qt.NoBrush)
        outline.setStrokeColor(QColor(outline_hex))
        outline.setStrokeWidth(outline_mm)
        outline.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
        sym.appendSymbolLayer(outline)

    return sym


def _stipple_fill_symbol(
    dot_hex: str,
    spacing_mm: float = 2.8,
    dot_mm: float = 0.28,
    *,
    bg_hex: str | None = None,
    outline_hex: str | None = None,
    outline_mm: float = 0.0,
) -> Any:
    """Build a subtle vector dot-stipple fill for scrub/sand in B&W mode."""
    from qgis.core import (
        QgsFillSymbol,
        QgsMarkerSymbol,
        QgsPointPatternFillSymbolLayer,
        QgsSimpleFillSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)

    if bg_hex is not None:
        bg = QgsSimpleFillSymbolLayer(QColor(bg_hex))
        bg.setStrokeStyle(Qt.NoPen)
        sym.appendSymbolLayer(bg)

    pattern = QgsPointPatternFillSymbolLayer()
    pattern.setDistanceX(spacing_mm)
    pattern.setDistanceY(spacing_mm)
    pattern.setDistanceXUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDistanceYUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDisplacementX(spacing_mm * 0.5)
    pattern.setDisplacementXUnit(QgsUnitTypes.RenderMillimeters)

    marker = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": dot_hex,
            "outline_style": "no",
            "size": f"{dot_mm:.3f}",
            "size_unit": "MM",
        }
    )
    pattern.setSubSymbol(marker)
    sym.appendSymbolLayer(pattern)

    if outline_hex is not None and outline_mm > 0:
        outline = QgsSimpleFillSymbolLayer()
        outline.setBrushStyle(Qt.NoBrush)
        outline.setStrokeColor(QColor(outline_hex))
        outline.setStrokeWidth(outline_mm)
        outline.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
        sym.appendSymbolLayer(outline)

    return sym


def _line_symbol(
    color_hex: str,
    width_mm: float,
    *,
    dash_vector: list[float] | None = None,
    cap_round: bool = True,
    render_pass: int = 0,
) -> Any:
    from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)
    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), width_mm)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.RoundCap if cap_round else Qt.FlatCap)
    sl.setPenJoinStyle(Qt.RoundJoin)
    if dash_vector:
        sl.setUseCustomDashPattern(True)
        sl.setCustomDashVector(dash_vector)
        sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sl.setRenderingPass(render_pass)
    sym.appendSymbolLayer(sl)
    return sym


def _cased_road_symbol(
    casing_hex: str,
    casing_mm: float,
    fill_hex: str,
    fill_mm: float,
    *,
    casing_pass: int,
    fill_pass: int,
    centerline_hex: str | None = None,
    centerline_mm: float = 0.0,
    centerline_pass: int = 28,
) -> Any:
    """Create a multi-layer road symbol with explicit symbol-level rendering passes."""
    from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    casing = QgsSimpleLineSymbolLayer(QColor(casing_hex), casing_mm)
    casing.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    casing.setPenCapStyle(Qt.RoundCap)
    casing.setPenJoinStyle(Qt.RoundJoin)
    casing.setRenderingPass(casing_pass)
    sym.appendSymbolLayer(casing)

    fill = QgsSimpleLineSymbolLayer(QColor(fill_hex), fill_mm)
    fill.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    fill.setPenCapStyle(Qt.RoundCap)
    fill.setPenJoinStyle(Qt.RoundJoin)
    fill.setRenderingPass(fill_pass)
    sym.appendSymbolLayer(fill)

    if centerline_hex is not None and centerline_mm > 0:
        center = QgsSimpleLineSymbolLayer(QColor(centerline_hex), centerline_mm)
        center.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        center.setPenCapStyle(Qt.FlatCap)
        center.setPenJoinStyle(Qt.RoundJoin)
        center.setRenderingPass(centerline_pass)
        sym.appendSymbolLayer(center)

    return sym


def _railway_symbol(color_hex: str = "#1A1A1A", base_pass: int = 15) -> Any:
    """Create classic topographic railway line with perpendicular cross-hatch ticks."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsMarkerSymbol,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    rail = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.30)
    rail.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    rail.setPenCapStyle(Qt.FlatCap)
    rail.setPenJoinStyle(Qt.RoundJoin)
    rail.setRenderingPass(base_pass)
    sym.appendSymbolLayer(rail)

    ticks = QgsMarkerLineSymbolLayer(True, 3.2)
    ticks.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    ticks.setRenderingPass(base_pass + 1)

    tick_marker = QgsMarkerSymbol.createSimple(
        {
            "name": "line",
            "color": color_hex,
            "outline_color": color_hex,
            "outline_width": "0.24",
            "outline_width_unit": "MM",
            "size": "1.35",
            "size_unit": "MM",
            "angle": "0",
        }
    )
    ticks.setSubSymbol(tick_marker)
    sym.appendSymbolLayer(ticks)
    return sym


def _powerline_symbol(color_hex: str, render_pass: int = 2) -> Any:
    """Thin dashed power line with subtle dot pylons."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsMarkerSymbol,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.16)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.FlatCap)
    sl.setUseCustomDashPattern(True)
    sl.setCustomDashVector([2.4, 1.1])
    sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sl.setRenderingPass(render_pass)
    sym.appendSymbolLayer(sl)

    dots = QgsMarkerLineSymbolLayer(False, 11.0)
    dots.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    dots.setRenderingPass(render_pass)
    dot_marker = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": color_hex,
            "outline_style": "no",
            "size": "0.55",
            "size_unit": "MM",
        }
    )
    dots.setSubSymbol(dot_marker)
    sym.appendSymbolLayer(dots)
    return sym


def _add_rule(
    root_rule: Any,
    label: str,
    filter_expr: str,
    symbol: Any,
) -> Any:
    from qgis.core import QgsRuleBasedRenderer

    rule = QgsRuleBasedRenderer.Rule(symbol)
    rule.setLabel(label)
    rule.setFilterExpression(filter_expr)
    root_rule.appendChild(rule)
    return rule


def _configure_curved_line_pal(
    pal: Any,
    *,
    on_line: bool = True,
    repeat_mm: float = 0.0,
    min_feature_mm: float = 12.0,
) -> None:
    """Configure curved line PAL placement safely across QGIS 3.34-3.40 APIs."""
    from qgis.core import Qgis, QgsPalLayerSettings, QgsUnitTypes

    try:
        pal.placement = Qgis.LabelPlacement.Curved
    except AttributeError:
        pal.placement = QgsPalLayerSettings.Curved

    if hasattr(pal, "lineSettings"):
        ls = pal.lineSettings()
        try:
            flag = (
                Qgis.LabelLinePlacementFlag.OnLine
                if on_line
                else Qgis.LabelLinePlacementFlag.AboveLine
            )
            ls.setPlacementFlags(flag)
        except (AttributeError, TypeError):
            try:
                pal.placementFlags = (
                    QgsPalLayerSettings.OnLine if on_line else QgsPalLayerSettings.AboveLine
                )
            except AttributeError:
                pass
        if hasattr(ls, "setMergeLines"):
            ls.setMergeLines(True)
        if hasattr(pal, "setLineSettings"):
            pal.setLineSettings(ls)
    else:
        pal.placementFlags = (
            QgsPalLayerSettings.OnLine if on_line else QgsPalLayerSettings.AboveLine
        )

    if hasattr(pal, "mergeLines"):
        pal.mergeLines = True

    if repeat_mm > 0:
        pal.repeatDistance = repeat_mm
        pal.repeatDistanceUnit = QgsUnitTypes.RenderMillimeters

    if hasattr(pal, "thinningSettings"):
        ts = pal.thinningSettings()
        if hasattr(ts, "setMinimumFeatureSize"):
            ts.setMinimumFeatureSize(min_feature_mm)
        if hasattr(pal, "setThinningSettings"):
            pal.setThinningSettings(ts)
    if hasattr(pal, "minimumFeatureSize"):
        try:
            pal.minimumFeatureSize = min_feature_mm
        except AttributeError:
            pass


def _configure_point_pal(pal: Any, *, over_point: bool = False, dist_mm: float = 1.4) -> None:
    from qgis.core import Qgis, QgsPalLayerSettings, QgsUnitTypes

    if over_point:
        try:
            pal.placement = Qgis.LabelPlacement.OverPoint
        except AttributeError:
            pal.placement = QgsPalLayerSettings.OverPoint
        pal.dist = 0.0
    else:
        try:
            pal.placement = Qgis.LabelPlacement.AroundPoint
        except AttributeError:
            pal.placement = QgsPalLayerSettings.AroundPoint
        pal.dist = dist_mm
        pal.distUnits = QgsUnitTypes.RenderMillimeters


def _build_landcover_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsRuleBasedRenderer

    root = QgsRuleBasedRenderer.Rule(None)
    if color_mode == ColorMode.COLOR:
        _add_rule(
            root,
            "Coniferous Forest",
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND \"leaf_type\" = 'needleleaved'",
            _simple_fill_symbol("#C4DCBA"),
        )
        _add_rule(
            root,
            "Broadleaf / Mixed Forest",
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND (\"leaf_type\" IS NULL OR \"leaf_type\" != 'needleleaved')",
            _simple_fill_symbol("#CFE2C2"),
        )
        _add_rule(
            root,
            "Scrub / Heath / Grassland",
            "\"natural\" IN ('scrub','heath','grassland','fell') "
            "OR \"landuse\" IN ('meadow','grass','orchard','vineyard','farmland')",
            _simple_fill_symbol("#E3ECD2"),
        )
        _add_rule(
            root,
            "Parks & Reserves",
            "\"leisure\" IN ('park','nature_reserve','golf_course','recreation_ground') "
            "OR \"landuse\" = 'recreation_ground'",
            _simple_fill_symbol("#D8EAD0", stroke_hex="#8FB884", stroke_mm=0.14, stroke_style="dash"),
        )
        _add_rule(
            root,
            "Wetland / Marsh",
            "\"natural\" IN ('wetland','mud')",
            _hatch_fill_symbol(
                "#5A96A8",
                angle_deg=0.0,
                spacing_mm=2.2,
                line_mm=0.14,
                bg_hex="#D2E5E2",
                dash_vector=[1.8, 1.4],
            ),
        )
        _add_rule(
            root,
            "Bare Rock / Sand / Scree",
            "\"natural\" IN ('bare_rock','scree','shingle','sand','beach')",
            _simple_fill_symbol("#EAE4D7"),
        )
        _add_rule(
            root,
            "Built-up Residential / Commercial",
            "\"landuse\" IN ('residential','commercial','industrial','retail')",
            _simple_fill_symbol("#EFECE5"),
        )
        _add_rule(
            root,
            "Military Area",
            "\"landuse\" = 'military'",
            _hatch_fill_symbol(
                "#C06060",
                angle_deg=45.0,
                spacing_mm=3.0,
                line_mm=0.14,
                bg_hex="#F4E8E6",
                outline_hex="#B04A4A",
                outline_mm=0.22,
            ),
        )
        _add_rule(
            root,
            "Aeroway Area",
            "\"aeroway\" IN ('aerodrome','runway','apron','taxiway')",
            _simple_fill_symbol("#E2E0DA", stroke_hex="#666666", stroke_mm=0.18, stroke_style="solid"),
        )
    else:
        # Dedicated high-contrast B&W laser-print theme: crisp hatches/stipples, zero gray mud.
        _add_rule(
            root,
            "Forest / Woods",
            "\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')",
            _hatch_fill_symbol("#969696", angle_deg=45.0, spacing_mm=2.0, line_mm=0.14),
        )
        _add_rule(
            root,
            "Scrub / Heath / Grassland",
            "\"natural\" IN ('scrub','heath','grassland','fell') "
            "OR \"landuse\" IN ('meadow','grass','orchard','vineyard')",
            _stipple_fill_symbol("#8C8C8C", spacing_mm=3.0, dot_mm=0.30),
        )
        _add_rule(
            root,
            "Parks & Reserves",
            "\"leisure\" IN ('park','nature_reserve','golf_course','recreation_ground') "
            "OR \"landuse\" = 'recreation_ground'",
            _simple_fill_symbol("transparent", stroke_hex="#666666", stroke_mm=0.18, stroke_style="dash"),
        )
        _add_rule(
            root,
            "Wetland / Marsh",
            "\"natural\" IN ('wetland','mud')",
            _hatch_fill_symbol("#7A7A7A", angle_deg=0.0, spacing_mm=2.2, line_mm=0.14, dash_vector=[1.8, 1.4]),
        )
        _add_rule(
            root,
            "Bare Rock / Sand",
            "\"natural\" IN ('bare_rock','scree','shingle','sand','beach')",
            _stipple_fill_symbol("#999999", spacing_mm=2.4, dot_mm=0.24),
        )
        _add_rule(
            root,
            "Military Area",
            "\"landuse\" = 'military'",
            _hatch_fill_symbol(
                "#555555",
                angle_deg=135.0,
                spacing_mm=3.2,
                line_mm=0.16,
                outline_hex="#222222",
                outline_mm=0.26,
            ),
        )
        _add_rule(
            root,
            "Aeroway Area",
            "\"aeroway\" IN ('aerodrome','runway','apron','taxiway')",
            _simple_fill_symbol("#ECECEC", stroke_hex="#222222", stroke_mm=0.22, stroke_style="solid"),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLabelsEnabled(False)


def _build_hillshade_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsBilinearRasterResampler, QgsSingleBandGrayRenderer
    from qgis.PyQt.QtGui import QPainter

    renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    if color_mode == ColorMode.COLOR:
        renderer.setOpacity(0.32)
    else:
        # Dedicated B&W laser-print contrast curve: light, airy shading, never black mud!
        renderer.setOpacity(0.20)
    layer.setRenderer(renderer)
    layer.setBlendMode(QPainter.CompositionMode_Multiply)

    if layer.resampleFilter() is not None:
        layer.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        layer.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())

    if layer.brightnessFilter() is not None:
        if color_mode == ColorMode.COLOR:
            layer.brightnessFilter().setBrightness(8)
            layer.brightnessFilter().setContrast(5)
        else:
            layer.brightnessFilter().setBrightness(38)
            layer.brightnessFilter().setContrast(-6)


def _build_water_polygons_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    water_filter = (
        "\"natural\" = 'water' OR \"water\" IS NOT NULL "
        "OR \"waterway\" IN ('riverbank','dock','canal') "
        "OR \"landuse\" IN ('reservoir','basin')"
    )
    root = QgsRuleBasedRenderer.Rule(None)
    if color_mode == ColorMode.COLOR:
        sym = _simple_fill_symbol("#A6D1EC", stroke_hex="#3A82B5", stroke_mm=0.20, stroke_style="solid")
    else:
        sym = _hatch_fill_symbol(
            "#6B6B6B",
            angle_deg=0.0,
            spacing_mm=1.25,
            line_mm=0.12,
            bg_hex="#FFFFFF",
            outline_hex="#1A1A1A",
            outline_mm=0.26,
        )
    _add_rule(root, "Waterbodies", water_filter, sym)
    layer.setRenderer(QgsRuleBasedRenderer(root))

    # Label named water bodies
    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 5
    pal.setFormat(
        _make_text_format(
            6.5,
            "#1E5B88" if color_mode == ColorMode.COLOR else "#111111",
            italic=True,
            halo_hex="#FFFFFF",
            halo_mm=0.6,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Waterbody Names")
    rule.setFilterExpression(f"({water_filter}) AND \"name\" IS NOT NULL AND length(\"name\") > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)
    _ = QgsVectorLayerSimpleLabeling  # keep reference available


def _build_contours_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    root = QgsRuleBasedRenderer.Rule(None)
    if color_mode == ColorMode.COLOR:
        idx_color, minor_color = "#8F5B28", "#A87846"
        idx_w, minor_w = 0.32, 0.15
        label_color, halo_color = "#78481B", "#FAF9F5"
    else:
        idx_color, minor_color = "#1A1A1A", "#505050"
        idx_w, minor_w = 0.30, 0.14
        label_color, halo_color = "#111111", "#FFFFFF"

    _add_rule(root, "Index Contours", "\"is_index\" = 1", _line_symbol(idx_color, idx_w))
    _add_rule(
        root,
        "Intermediate Contours",
        "\"is_index\" = 0 OR \"is_index\" IS NULL",
        _line_symbol(minor_color, minor_w),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    # Curved PAL elevation labels on index contours only, sitting ON the line with halo
    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "elev_label"
    pal.isExpression = False
    pal.priority = 3
    _configure_curved_line_pal(pal, on_line=True, repeat_mm=55.0, min_feature_mm=12.0)
    pal.setFormat(
        _make_text_format(
            5.8,
            label_color,
            bold=True,
            halo_hex=halo_color,
            halo_mm=0.65,
            halo_opacity=0.95,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Index Contour Elevations")
    rule.setFilterExpression("\"is_index\" = 1 AND \"elev_label\" IS NOT NULL")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_waterways_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    root = QgsRuleBasedRenderer.Rule(None)
    if color_mode == ColorMode.COLOR:
        river_c, stream_c, label_c = "#2B79B3", "#3D8AC4", "#1E5B88"
        halo_c = "#FAF9F5"
    else:
        river_c, stream_c, label_c = "#1A1A1A", "#333333", "#111111"
        halo_c = "#FFFFFF"

    _add_rule(
        root,
        "Rivers & Canals",
        "\"waterway\" IN ('river','canal') AND (\"intermittent\" IS NULL OR \"intermittent\" != 'yes')",
        _line_symbol(river_c, 0.55),
    )
    _add_rule(
        root,
        "Streams & Creeks",
        "\"waterway\" IN ('stream','creek','drain','ditch') AND (\"intermittent\" IS NULL OR \"intermittent\" != 'yes')",
        _line_symbol(stream_c, 0.24),
    )
    _add_rule(
        root,
        "Intermittent Waterways",
        "\"waterway\" IN ('river','stream','creek','canal','drain','ditch') AND \"intermittent\" = 'yes'",
        _line_symbol(stream_c, 0.22, dash_vector=[2.8, 0.9, 0.5, 0.9]),
    )
    _add_rule(
        root,
        "Dams & Weirs",
        "\"waterway\" IN ('dam','weir','waterfall')",
        _line_symbol(river_c, 0.45, cap_round=False),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=75.0, min_feature_mm=16.0)
    pal.setFormat(_make_text_format(6.0, label_c, italic=True, halo_hex=halo_c, halo_mm=0.55))
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Waterway Names")
    rule.setFilterExpression(
        "\"waterway\" IN ('river','stream','creek','canal') AND \"name\" IS NOT NULL AND length(\"name\") > 0"
    )
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_buildings_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsRuleBasedRenderer

    root = QgsRuleBasedRenderer.Rule(None)
    if color_mode == ColorMode.COLOR:
        sym = _simple_fill_symbol("#6E6E6E", stroke_hex="#404040", stroke_mm=0.10, stroke_style="solid")
    else:
        sym = _simple_fill_symbol("#262626", stroke_hex="#000000", stroke_mm=0.10, stroke_style="solid")
    _add_rule(root, "Buildings", "\"building\" IS NOT NULL AND \"building\" != 'no'", sym)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLabelsEnabled(False)


def _build_transport_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    root = QgsRuleBasedRenderer.Rule(None)
    is_color = color_mode == ColorMode.COLOR

    if is_color:
        _add_rule(
            root,
            "Motorway / Trunk",
            "\"highway\" IN ('motorway','motorway_link','trunk','trunk_link')",
            _cased_road_symbol(
                "#5E1D12",
                1.40,
                "#C84B31",
                0.95,
                casing_pass=12,
                fill_pass=24,
                centerline_hex="#FAF9F5",
                centerline_mm=0.12,
                centerline_pass=26,
            ),
        )
        _add_rule(
            root,
            "Primary Road",
            "\"highway\" IN ('primary','primary_link')",
            _cased_road_symbol("#6E3A0C", 1.15, "#D97925", 0.75, casing_pass=10, fill_pass=22),
        )
        _add_rule(
            root,
            "Secondary Road",
            "\"highway\" IN ('secondary','secondary_link')",
            _cased_road_symbol("#6E5614", 0.95, "#E5B742", 0.60, casing_pass=8, fill_pass=20),
        )
        _add_rule(
            root,
            "Tertiary / Residential",
            "\"highway\" IN ('tertiary','tertiary_link','unclassified','residential','living_street','road')",
            _cased_road_symbol("#4A4A4A", 0.72, "#FFFFFF", 0.44, casing_pass=6, fill_pass=18),
        )
        _add_rule(
            root,
            "Service Road",
            "\"highway\" = 'service'",
            _cased_road_symbol("#686868", 0.46, "#F7F7F7", 0.26, casing_pass=4, fill_pass=16),
        )
        _add_rule(
            root,
            "Unpaved Track / 4WD",
            "\"highway\" = 'track'",
            _line_symbol("#5E442B", 0.34, dash_vector=[2.2, 1.0], render_pass=5),
        )
        _add_rule(
            root,
            "Trails / Footways / Paths",
            "\"highway\" IN ('path','footway','bridleway','cycleway','steps')",
            _line_symbol("#3D3D3D", 0.24, dash_vector=[1.1, 0.9], render_pass=3),
        )
        _add_rule(
            root,
            "Railway",
            "\"railway\" IN ('rail','light_rail','narrow_gauge','preserved','tram')",
            _railway_symbol("#1A1A1A", base_pass=15),
        )
        _add_rule(
            root,
            "Runways & Taxiways",
            "\"aeroway\" IN ('runway','taxiway')",
            _cased_road_symbol(
                "#222222", 1.15, "#9C9C9C", 0.75, casing_pass=7, fill_pass=19, centerline_hex="#FFFFFF", centerline_mm=0.12
            ),
        )
        _add_rule(
            root,
            "Power Lines",
            "\"power\" IN ('line','minor_line')",
            _powerline_symbol("#666666", render_pass=2),
        )
        _add_rule(
            root,
            "Pipelines",
            "\"man_made\" = 'pipeline'",
            _line_symbol("#555555", 0.20, dash_vector=[2.0, 0.8, 0.4, 0.8], render_pass=2),
        )
    else:
        # High-contrast B&W laser-print transport hierarchy
        _add_rule(
            root,
            "Motorway / Trunk",
            "\"highway\" IN ('motorway','motorway_link','trunk','trunk_link')",
            _cased_road_symbol(
                "#000000",
                1.45,
                "#FFFFFF",
                0.88,
                casing_pass=12,
                fill_pass=24,
                centerline_hex="#000000",
                centerline_mm=0.22,
                centerline_pass=26,
            ),
        )
        _add_rule(
            root,
            "Primary Road",
            "\"highway\" IN ('primary','primary_link')",
            _cased_road_symbol("#000000", 1.15, "#C8C8C8", 0.68, casing_pass=10, fill_pass=22),
        )
        _add_rule(
            root,
            "Secondary Road",
            "\"highway\" IN ('secondary','secondary_link')",
            _cased_road_symbol("#000000", 0.95, "#FFFFFF", 0.56, casing_pass=8, fill_pass=20),
        )
        _add_rule(
            root,
            "Tertiary / Residential",
            "\"highway\" IN ('tertiary','tertiary_link','unclassified','residential','living_street','road')",
            _cased_road_symbol("#222222", 0.68, "#FFFFFF", 0.40, casing_pass=6, fill_pass=18),
        )
        _add_rule(
            root,
            "Service Road",
            "\"highway\" = 'service'",
            _line_symbol("#444444", 0.28, render_pass=4),
        )
        _add_rule(
            root,
            "Unpaved Track / 4WD",
            "\"highway\" = 'track'",
            _line_symbol("#1A1A1A", 0.34, dash_vector=[2.2, 1.0], render_pass=5),
        )
        _add_rule(
            root,
            "Trails / Footways / Paths",
            "\"highway\" IN ('path','footway','bridleway','cycleway','steps')",
            _line_symbol("#1A1A1A", 0.24, dash_vector=[0.85, 0.85], render_pass=3),
        )
        _add_rule(
            root,
            "Railway",
            "\"railway\" IN ('rail','light_rail','narrow_gauge','preserved','tram')",
            _railway_symbol("#000000", base_pass=15),
        )
        _add_rule(
            root,
            "Runways & Taxiways",
            "\"aeroway\" IN ('runway','taxiway')",
            _cased_road_symbol(
                "#000000", 1.15, "#B0B0B0", 0.72, casing_pass=7, fill_pass=19, centerline_hex="#FFFFFF", centerline_mm=0.14
            ),
        )
        _add_rule(
            root,
            "Power Lines",
            "\"power\" IN ('line','minor_line')",
            _powerline_symbol("#444444", render_pass=2),
        )
        _add_rule(
            root,
            "Pipelines",
            "\"man_made\" = 'pipeline'",
            _line_symbol("#333333", 0.20, dash_vector=[2.0, 0.8, 0.4, 0.8], render_pass=2),
        )

    renderer = QgsRuleBasedRenderer(root)
    renderer.setUsingSymbolLevels(True)
    layer.setRenderer(renderer)

    # Road labels (major roads get ref + name; minor roads get name)
    halo_hex = "#FAF9F5" if is_color else "#FFFFFF"
    label_root = QgsRuleBasedLabeling.Rule(None)

    pal_major = QgsPalLayerSettings()
    pal_major.fieldName = (
        "CASE WHEN \"ref\" IS NOT NULL AND length(\"ref\") > 0 AND \"name\" IS NOT NULL AND length(\"name\") > 0 "
        "THEN \"ref\" || ' ' || \"name\" "
        "WHEN \"name\" IS NOT NULL AND length(\"name\") > 0 THEN \"name\" "
        "ELSE \"ref\" END"
    )
    pal_major.isExpression = True
    pal_major.priority = 7
    _configure_curved_line_pal(pal_major, on_line=False, repeat_mm=65.0, min_feature_mm=14.0)
    pal_major.setFormat(_make_text_format(6.4, "#1A1A1A", bold=True, halo_hex=halo_hex, halo_mm=0.7))
    r_major = QgsRuleBasedLabeling.Rule(pal_major)
    r_major.setDescription("Major Road Labels")
    r_major.setFilterExpression(
        "\"highway\" IN ('motorway','trunk','primary','secondary') "
        "AND ((\"name\" IS NOT NULL AND length(\"name\") > 0) OR (\"ref\" IS NOT NULL AND length(\"ref\") > 0))"
    )
    label_root.appendChild(r_major)

    pal_minor = QgsPalLayerSettings()
    pal_minor.fieldName = "name"
    pal_minor.isExpression = False
    pal_minor.priority = 5
    _configure_curved_line_pal(pal_minor, on_line=False, repeat_mm=70.0, min_feature_mm=15.0)
    pal_minor.setFormat(_make_text_format(5.6, "#2A2A2A" if is_color else "#111111", halo_hex=halo_hex, halo_mm=0.6))
    r_minor = QgsRuleBasedLabeling.Rule(pal_minor)
    r_minor.setDescription("Minor Road & Trail Labels")
    r_minor.setFilterExpression(
        "\"highway\" IN ('tertiary','unclassified','residential','track','path','footway') "
        "AND \"name\" IS NOT NULL AND length(\"name\") > 0"
    )
    label_root.appendChild(r_minor)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_points_style(layer: Any, color_mode: ColorMode, units: Units = Units.METRIC) -> None:
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    is_color = color_mode == ColorMode.COLOR
    halo_hex = "#FAF9F5" if is_color else "#FFFFFF"
    root = QgsRuleBasedRenderer.Rule(None)

    peak_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "triangle",
            "color": "#111111",
            "outline_color": "#FFFFFF",
            "outline_width": "0.28",
            "outline_width_unit": "MM",
            "size": "2.3",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Peaks & Summits", "\"natural\" IN ('peak','volcano')", peak_sym)

    saddle_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "diamond",
            "color": "#333333" if is_color else "#111111",
            "outline_color": "#FFFFFF",
            "outline_width": "0.22",
            "outline_width_unit": "MM",
            "size": "1.5",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Saddles & Viewpoints", "\"natural\" = 'saddle' OR \"tourism\" = 'viewpoint'", saddle_sym)

    station_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#1A1A1A",
            "outline_color": "#FFFFFF",
            "outline_width": "0.25",
            "outline_width_unit": "MM",
            "size": "1.6",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Railway Stations", "\"railway\" IN ('station','halt')", station_sym)

    aero_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": "#FFFFFF",
            "outline_color": "#111111",
            "outline_width": "0.35",
            "outline_width_unit": "MM",
            "size": "2.2",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Helipads & Aerodromes", "\"aeroway\" IN ('helipad','aerodrome')", aero_sym)

    tower_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "cross2",
            "color": "#222222",
            "outline_color": "#222222",
            "outline_width": "0.26",
            "outline_width_unit": "MM",
            "size": "1.4",
            "size_unit": "MM",
        }
    )
    _add_rule(
        root,
        "Towers & Masts",
        "\"man_made\" IN ('tower','mast','communications_tower','water_tower','lighthouse')",
        tower_sym,
    )

    # Invisible placeholder for place labels so PAL always renders place names
    place_sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0,0", "outline_style": "no", "size": "0.01"})
    _add_rule(
        root,
        "Places",
        "\"place\" IN ('city','town','village','suburb','hamlet','locality')",
        place_sym,
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    # Hierarchical point labeling
    label_root = QgsRuleBasedLabeling.Rule(None)

    # 1. Cities & Towns (highest priority)
    pal_town = QgsPalLayerSettings()
    pal_town.fieldName = "upper(\"name\")"
    pal_town.isExpression = True
    pal_town.priority = 10
    _configure_point_pal(pal_town, over_point=True)
    pal_town.setFormat(_make_text_format(9.0, "#111111", bold=True, halo_hex=halo_hex, halo_mm=0.95))
    r_town = QgsRuleBasedLabeling.Rule(pal_town)
    r_town.setDescription("Cities & Towns")
    r_town.setFilterExpression("\"place\" IN ('city','town') AND \"name\" IS NOT NULL")
    label_root.appendChild(r_town)

    # 2. Villages & Suburbs
    pal_village = QgsPalLayerSettings()
    pal_village.fieldName = "name"
    pal_village.isExpression = False
    pal_village.priority = 9
    _configure_point_pal(pal_village, over_point=True)
    pal_village.setFormat(_make_text_format(7.6, "#1E1E1E", bold=True, halo_hex=halo_hex, halo_mm=0.85))
    r_village = QgsRuleBasedLabeling.Rule(pal_village)
    r_village.setDescription("Villages & Suburbs")
    r_village.setFilterExpression("\"place\" IN ('village','suburb') AND \"name\" IS NOT NULL")
    label_root.appendChild(r_village)

    # 3. Peaks (name + elevation in metric or imperial; parses "1012", "950.5", and "1012 m",
    #    and avoids duplicating elevation when OSM "name" already includes it, e.g. "Castle Cliff Trig (986 m)")
    num_ele = "to_real(regexp_substr(\"ele\", '-?[0-9]+(?:\\\\.[0-9]+)?'))"
    if units == Units.IMPERIAL:
        rounded_val = f"to_int(round({num_ele} * 3.280839895))"
        ele_expr = f"{rounded_val} || ' ft'"
        unit_pat = "(?:ft|feet)"
    else:
        rounded_val = f"to_int(round({num_ele}))"
        ele_expr = f"{rounded_val} || ' m'"
        unit_pat = "m(?:eters?|etres?)?"
    already_has_ele = (
        f"regexp_match(\"name\", '(?i)(?:^|[^0-9])' || to_string({rounded_val}) || '\\\\s*{unit_pat}\\\\b') > 0"
    )
    peak_expr = (
        f"CASE "
        f"WHEN \"name\" IS NOT NULL AND \"ele\" IS NOT NULL AND {num_ele} IS NOT NULL "
        f"THEN CASE WHEN {already_has_ele} THEN \"name\" ELSE \"name\" || '\\n' || {ele_expr} END "
        f"WHEN \"name\" IS NOT NULL THEN \"name\" "
        f"WHEN \"ele\" IS NOT NULL AND {num_ele} IS NOT NULL THEN {ele_expr} "
        f"ELSE '' END"
    )
    pal_peak = QgsPalLayerSettings()
    pal_peak.fieldName = peak_expr
    pal_peak.isExpression = True
    pal_peak.priority = 8
    _configure_point_pal(pal_peak, over_point=False, dist_mm=1.6)
    pal_peak.setFormat(_make_text_format(6.8, "#111111", bold=True, halo_hex=halo_hex, halo_mm=0.75))
    r_peak = QgsRuleBasedLabeling.Rule(pal_peak)
    r_peak.setDescription("Peaks & Elevations")
    r_peak.setFilterExpression("\"natural\" IN ('peak','volcano') AND (\"name\" IS NOT NULL OR \"ele\" IS NOT NULL)")
    label_root.appendChild(r_peak)

    # 4. Hamlets, Localities, Viewpoints, Stations
    pal_local = QgsPalLayerSettings()
    pal_local.fieldName = "name"
    pal_local.isExpression = False
    pal_local.priority = 6
    _configure_point_pal(pal_local, over_point=False, dist_mm=1.3)
    pal_local.setFormat(_make_text_format(6.4, "#2B2B2B" if is_color else "#111111", halo_hex=halo_hex, halo_mm=0.65))
    r_local = QgsRuleBasedLabeling.Rule(pal_local)
    r_local.setDescription("Localities / Viewpoints / Stations")
    r_local.setFilterExpression(
        "(\"place\" IN ('hamlet','locality') OR \"tourism\" = 'viewpoint' OR \"railway\" IN ('station','halt')) "
        "AND \"name\" IS NOT NULL"
    )
    label_root.appendChild(r_local)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_mgrs_grid_1km_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsSingleSymbolRenderer
    from qgis.PyQt.QtGui import QPainter

    if color_mode == ColorMode.COLOR:
        sym = _line_symbol("#1B4F72", 0.18, cap_round=False)
    else:
        sym = _line_symbol("#000000", 0.20, cap_round=False)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    layer.setOpacity(1.0)
    layer.setLabelsEnabled(False)


def _build_mgrs_grid_100km_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsSingleSymbolRenderer
    from qgis.PyQt.QtGui import QPainter

    if color_mode == ColorMode.COLOR:
        sym = _line_symbol("#10324A", 0.45, cap_round=False)
    else:
        sym = _line_symbol("#000000", 0.50, cap_round=False)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    layer.setOpacity(1.0)
    layer.setLabelsEnabled(False)


def _build_mgrs_zone_boundary_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsSingleSymbolRenderer

    if color_mode == ColorMode.COLOR:
        sym = _line_symbol("#7B1FA2", 0.55, dash_vector=[3.0, 1.4], cap_round=False)
    else:
        sym = _line_symbol("#000000", 0.60, dash_vector=[3.0, 1.4], cap_round=False)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_mgrs_square_labels_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    # Invisible point marker; render subtle haloed 100 km square ID badge
    sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0,0", "outline_style": "no", "size": "0.01"})
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    pal = QgsPalLayerSettings()
    pal.fieldName = "square_id"
    pal.isExpression = False
    pal.priority = 2
    _configure_point_pal(pal, over_point=True)
    pal.setFormat(
        _make_text_format(
            11.0,
            "#1B4F72" if color_mode == ColorMode.COLOR else "#1A1A1A",
            bold=True,
            halo_hex="#FAF9F5" if color_mode == ColorMode.COLOR else "#FFFFFF",
            halo_mm=1.0,
            halo_opacity=0.85,
            opacity=0.68,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_overlay_style(layer: Any, role: str, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    is_color = color_mode == ColorMode.COLOR
    accent = "#C2185B" if is_color else "#000000"

    if role == "overlay_polygons":
        sym = _simple_fill_symbol(
            accent if is_color else "transparent",
            stroke_hex=accent,
            stroke_mm=0.45 if is_color else 0.55,
            stroke_style="dash",
            fill_opacity=0.14 if is_color else 1.0,
        )
    elif role == "overlay_lines":
        sym = _line_symbol(accent, 0.55 if is_color else 0.65, dash_vector=[2.5, 1.1])
    else:
        sym = QgsMarkerSymbol.createSimple(
            {
                "name": "diamond",
                "color": accent if is_color else "#FFFFFF",
                "outline_color": "#FFFFFF" if is_color else "#000000",
                "outline_width": "0.35" if is_color else "0.50",
                "outline_width_unit": "MM",
                "size": "2.8",
                "size_unit": "MM",
            }
        )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    # Label overlay features if a "name" field is present
    field_names = {f.name().lower(): f.name() for f in layer.fields()}
    if "name" in field_names:
        pal = QgsPalLayerSettings()
        pal.fieldName = field_names["name"]
        pal.isExpression = False
        pal.priority = 9
        if role == "overlay_points":
            _configure_point_pal(pal, over_point=False, dist_mm=1.8)
        pal.setFormat(_make_text_format(6.8, accent, bold=True, halo_hex="#FFFFFF", halo_mm=0.75))
        layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        layer.setLabelsEnabled(True)
    else:
        layer.setLabelsEnabled(False)


def apply_programmatic_style(
    layer: Any,
    role: str,
    color_mode: ColorMode,
    units: Units = Units.METRIC,
) -> None:
    """Apply GridForge's programmatic PyQGIS 3.40 style for ``role``."""
    if role == "landcover":
        _build_landcover_style(layer, color_mode)
    elif role == "hillshade":
        _build_hillshade_style(layer, color_mode)
    elif role == "water_polygons":
        _build_water_polygons_style(layer, color_mode)
    elif role == "contours":
        _build_contours_style(layer, color_mode)
    elif role == "waterways":
        _build_waterways_style(layer, color_mode)
    elif role == "buildings":
        _build_buildings_style(layer, color_mode)
    elif role == "transport":
        _build_transport_style(layer, color_mode)
    elif role == "points":
        _build_points_style(layer, color_mode, units=units)
    elif role == "mgrs_grid_1km":
        _build_mgrs_grid_1km_style(layer, color_mode)
    elif role == "mgrs_grid_100km":
        _build_mgrs_grid_100km_style(layer, color_mode)
    elif role == "mgrs_zone_boundary":
        _build_mgrs_zone_boundary_style(layer, color_mode)
    elif role == "mgrs_square_labels":
        _build_mgrs_square_labels_style(layer, color_mode)
    elif role in ("overlay_polygons", "overlay_lines", "overlay_points"):
        _build_overlay_style(layer, role, color_mode)
    else:
        raise ValueError(f"Unknown GridForge style role: {role!r}")


_LABELED_ROLES: frozenset[str] = frozenset(
    {"water_polygons", "contours", "waterways", "transport", "points", "mgrs_square_labels"}
)
_NATIVE_QML_MARKER = "gridforge-pyqgis-exported"


def apply_layer_style(
    layer: Any,
    role: str,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    styles_root: Path | None = None,
    prefer_qml: bool = True,
) -> bool:
    """Apply styling for ``role`` from versioned ``.qml`` with programmatic fallback.

    Returns ``True`` if loaded from a native QGIS 3.40 ``.qml`` file, ``False`` if applied
    programmatically. Note: ``points`` in Imperial mode applies dynamic ft conversion
    programmatically so peak elevation labels always match ``spec.units``.
    """
    qml_path = get_qml_path(role, color_mode, styles_root=styles_root)
    use_qml = (
        prefer_qml
        and qml_path.is_file()
        and not (role == "points" and units == Units.IMPERIAL)
        and _NATIVE_QML_MARKER in qml_path.read_text(encoding="utf-8", errors="ignore")[:512]
    )

    if use_qml:
        try:
            msg, ok = layer.loadNamedStyle(str(qml_path))
            renderer_ok = ok and layer.renderer() is not None
            labeling_ok = (role not in _LABELED_ROLES) or (
                layer.labelsEnabled() and layer.labeling() is not None
            )
            if renderer_ok and labeling_ok:
                # Re-assert bilinear resampling & Multiply blend for hillshade
                if role == "hillshade":
                    _build_hillshade_style(layer, color_mode)
                return True
            logger.warning(
                "QML style %s incomplete (ok=%s, renderer=%s, labeling=%s, msg=%s); "
                "falling back to programmatic style",
                qml_path,
                ok,
                layer.renderer() is not None,
                labeling_ok,
                msg,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning("Exception loading QML %s (%s); falling back to programmatic style", qml_path, exc)

    apply_programmatic_style(layer, role, color_mode, units=units)
    return False


def export_qml_styles(styles_root: Path | None = None) -> list[Path]:
    """Generate native QGIS 3.40 ``.qml`` files for both Color and B&W themes.

    Must be executed with an initialized ``QgsApplication``.
    """
    import tempfile

    from osgeo import gdal
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    root = styles_root if styles_root is not None else get_styles_root()
    written: list[Path] = []

    vector_specs: dict[str, tuple[str, str]] = {
        "landcover": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=name:string&field=natural:string&field=landuse:string"
            "&field=leaf_type:string&field=water:string&field=waterway:string"
            "&field=building:string&field=amenity:string&field=leisure:string"
            "&field=aeroway:string&field=boundary:string",
            "landcover",
        ),
        "water_polygons": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=name:string&field=natural:string&field=landuse:string"
            "&field=water:string&field=waterway:string",
            "water_polygons",
        ),
        "contours": (
            "LineString?crs=EPSG:32756&field=elev:double&field=elev_label:string&field=is_index:integer",
            "contours",
        ),
        "waterways": (
            "LineString?crs=EPSG:32756&field=name:string&field=waterway:string&field=intermittent:string",
            "waterways",
        ),
        "buildings": (
            "MultiPolygon?crs=EPSG:32756&field=name:string&field=building:string",
            "buildings",
        ),
        "transport": (
            "LineString?crs=EPSG:32756"
            "&field=name:string&field=highway:string&field=ref:string&field=surface:string"
            "&field=tracktype:string&field=railway:string&field=aeroway:string"
            "&field=power:string&field=man_made:string&field=bridge:string"
            "&field=tunnel:string&field=layer:string&field=oneway:string",
            "transport",
        ),
        "points": (
            "Point?crs=EPSG:32756"
            "&field=name:string&field=highway:string&field=railway:string&field=aeroway:string"
            "&field=natural:string&field=ele:string&field=amenity:string&field=place:string"
            "&field=power:string&field=man_made:string&field=tourism:string",
            "points",
        ),
        "mgrs_grid_1km": (
            "LineString?crs=EPSG:32756&field=label:string&field=axis:string&field=coord_m:integer&field=is_100km:integer",
            "mgrs_grid_1km",
        ),
        "mgrs_grid_100km": (
            "LineString?crs=EPSG:32756&field=label:string&field=axis:string&field=coord_m:integer",
            "mgrs_grid_100km",
        ),
        "mgrs_zone_boundary": ("LineString?crs=EPSG:32756&field=label:string", "mgrs_zone_boundary"),
        "mgrs_square_labels": (
            "Point?crs=EPSG:32756&field=gzd:string&field=square_id:string&field=full_id:string",
            "mgrs_square_labels",
        ),
        "overlay_polygons": ("MultiPolygon?crs=EPSG:32756&field=name:string", "overlay_polygons"),
        "overlay_lines": ("LineString?crs=EPSG:32756&field=name:string", "overlay_lines"),
        "overlay_points": ("Point?crs=EPSG:32756&field=name:string", "overlay_points"),
    }

    with tempfile.TemporaryDirectory(prefix="gridforge_qml_") as tmpdir:
        gdal.UseExceptions()
        dummy_tif = Path(tmpdir) / "dummy_hillshade.tif"
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(str(dummy_tif), 4, 4, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        ds.GetRasterBand(1).Fill(190)
        ds.FlushCache()
        ds = None

        def _tag_native_qml(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            marker = f"<!-- {_NATIVE_QML_MARKER} -->\n"
            if _NATIVE_QML_MARKER not in text:
                if text.startswith("<!DOCTYPE"):
                    first_nl = text.find("\n")
                    text = text[: first_nl + 1] + marker + text[first_nl + 1 :]
                else:
                    text = marker + text
                path.write_text(text, encoding="utf-8")

        for mode in (ColorMode.COLOR, ColorMode.BW):
            mode_dir = root / mode.value
            mode_dir.mkdir(parents=True, exist_ok=True)

            # Hillshade raster QML
            rlayer = QgsRasterLayer(str(dummy_tif), "hillshade")
            apply_programmatic_style(rlayer, "hillshade", mode)
            hs_qml = mode_dir / "hillshade.qml"
            msg, ok = rlayer.saveNamedStyle(str(hs_qml))
            if not ok:
                raise RuntimeError(f"Failed to export {hs_qml}: {msg}")
            _tag_native_qml(hs_qml)
            written.append(hs_qml)

            # Vector QMLs
            for role, (uri, layer_name) in vector_specs.items():
                vlayer = QgsVectorLayer(uri, layer_name, "memory")
                apply_programmatic_style(vlayer, role, mode, units=Units.METRIC)
                qml_path = mode_dir / f"{role}.qml"
                msg, ok = vlayer.saveNamedStyle(str(qml_path))
                if not ok:
                    raise RuntimeError(f"Failed to export {qml_path}: {msg}")
                _tag_native_qml(qml_path)
                written.append(qml_path)

    return written


if __name__ == "__main__":
    import os
    import sys

    from qgis.core import QgsApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
    Path(os.environ["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        target = Path(sys.argv[1]) if len(sys.argv) > 1 else get_styles_root()
        paths = export_qml_styles(target)
        print(f"Exported {len(paths)} QGIS 3.40 QML files to {target}")
    finally:
        qgs.exitQgis()
