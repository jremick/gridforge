"""Phase 2 high-detail military cartographic styling for GridForge (Color & B&W).

Provides:
- Multi-scale symbology adapting across 1:15k, 1:24k/1:25k, and 1:48k/1:50k.
- Dedicated Color (classic NATO/USGS topographic) and B&W (monochrome laser-print)
  themes with zero grayscale desaturation.
- High-detail features: highway exits & shields, runways/helipads, power lines/towers,
  pipelines, SVG building/POI usage icons (points & building polygon centroids),
  downstream river flow arrows, separate evergreen/deciduous/mixed tree cover,
  hilltop elevation deduplication, and toggleable ``CartoOptions`` density controls.
- US public-domain regional overlay styling (FEMA NFHL flood zones, NHD hydrography,
  FAA/FCC obstacle towers, NOAA ENC depth contours & navaids).
- Reusable ``populate_project_layers(project, workspace, include_grid=...)`` shared
  by both print PDF rendering and Phase 3 collarless MBTiles/KMZ raster export.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gridforge.config import (
    AU_STATE_TRAIL_OVERLAY_KINDS,
    CartoOptions,
    ColorMode,
    DensityLevel,
    HillshadeStrength,
    JobWorkspace,
    NZOverlayKind,
    Units,
)

logger = logging.getLogger(__name__)

# Ordered roles matching the exact rendering pipeline (and exported native QMLs).
STYLE_ROLES: tuple[str, ...] = (
    "landcover",
    "hillshade",
    "viewshed",
    "slope_shading",
    "fema_flood",
    "au_protected_areas",
    "water_polygons",
    "au_hydro_polys",
    "contours",
    "enc_depth_contours",
    "nhd_flowlines",
    "au_hydro_lines",
    "waterways",
    "buildings",
    "transport",
    "au_state_trails",
    "au_power_lines",
    "au_pipelines",
    "au_runways",
    "building_icons",
    "points",
    "au_state_pois",
    "au_substations",
    "au_hydro_points",
    "au_homesteads",
    "au_airfields",
    "enc_navaids",
    "us_towers",
    "mgrs_grid_1km",
    "mgrs_grid_100km",
    "mgrs_zone_boundary",
    "mgrs_square_labels",
    "overlay_polygons",
    "overlay_lines",
    "overlay_points",
    "waypoint_route",
    "waypoint_points",
    "viewshed_rings",
    "viewshed_observers",
)

DEFAULT_FONT_FAMILY = "Liberation Sans"

_LABELED_ROLES: frozenset[str] = frozenset(
    {
        "fema_flood",
        "au_protected_areas",
        "water_polygons",
        "au_hydro_polys",
        "contours",
        "enc_depth_contours",
        "nhd_flowlines",
        "au_hydro_lines",
        "waterways",
        "transport",
        "au_state_trails",
        "au_power_lines",
        "au_pipelines",
        "points",
        "au_state_pois",
        "au_substations",
        "au_hydro_points",
        "au_homesteads",
        "au_airfields",
        "enc_navaids",
        "us_towers",
        "mgrs_square_labels",
        "waypoint_points",
        "viewshed_observers",
    }
)
_NATIVE_QML_MARKER = "gridforge-pyqgis-exported"
_DEFAULT_CARTO = CartoOptions()


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


def get_icons_root() -> Path:
    """Locate ``assets/icons`` relative to the repository or installed package."""
    candidates = [
        Path(__file__).resolve().parents[3] / "assets" / "icons",
        Path.cwd() / "assets" / "icons",
        Path("/app/assets/icons"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def get_icon_path(name: str, icons_root: Path | None = None) -> Path:
    """Return absolute path to a bundled military/topo SVG icon."""
    root = icons_root if icons_root is not None else get_icons_root()
    fname = name if name.endswith(".svg") else f"{name}.svg"
    return root / fname


def get_qml_path(role: str, color_mode: ColorMode, styles_root: Path | None = None) -> Path:
    """Return the versioned ``.qml`` path for a layer role and theme."""
    root = styles_root if styles_root is not None else get_styles_root()
    return root / color_mode.value / f"{role}.qml"


def map_background_hex(color_mode: ColorMode) -> str:
    """Return the map frame canvas background color hex for the chosen theme."""
    return "#FAF9F5" if color_mode == ColorMode.COLOR else "#FFFFFF"


def scale_width_factor(scale: int) -> float:
    """Return cartographic line/symbol width multiplier across supported scales."""
    if scale <= 15000:
        return 1.15
    if scale >= 48000:
        return 0.82
    return 1.0


def scale_font_delta(scale: int) -> float:
    """Return subtle point-size delta for multi-scale typography balance."""
    if scale <= 15000:
        return 0.4
    if scale >= 48000:
        return -0.5
    return 0.0


def _density_params(label_density: DensityLevel) -> tuple[float, float]:
    """Return ``(repeat_multiplier, min_feature_multiplier)`` for PAL labeling."""
    if label_density == DensityLevel.LOW:
        return (1.45, 1.50)
    if label_density == DensityLevel.HIGH:
        return (0.68, 0.58)
    return (1.0, 1.0)


def _hillshade_params(
    color_mode: ColorMode,
    strength: HillshadeStrength,
) -> tuple[float, int, int]:
    """Return ``(opacity, brightness, contrast)`` for hillshade Multiply blend."""
    if color_mode == ColorMode.COLOR:
        table = {
            HillshadeStrength.LIGHT: (0.18, 12, 2),
            HillshadeStrength.NORMAL: (0.32, 8, 5),
            HillshadeStrength.STRONG: (0.48, 0, 10),
        }
    else:
        table = {
            HillshadeStrength.LIGHT: (0.12, 46, -8),
            HillshadeStrength.NORMAL: (0.20, 38, -6),
            HillshadeStrength.STRONG: (0.30, 26, 4),
        }
    return table.get(strength, table[HillshadeStrength.NORMAL])


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
    badge_fill_hex: str | None = None,
    badge_stroke_hex: str | None = None,
) -> Any:
    """Construct a PyQGIS ``QgsTextFormat`` with crisp vector typography and optional shield badge."""
    from qgis.core import (
        QgsTextBackgroundSettings,
        QgsTextBufferSettings,
        QgsTextFormat,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import QSizeF
    from qgis.PyQt.QtGui import QColor, QFont

    font = QFont(font_family)
    font.setBold(bold)
    font.setItalic(italic)

    fmt = QgsTextFormat()
    fmt.setFont(font)
    fmt.setSize(max(4.5, size_pt))
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    fmt.setColor(QColor(color_hex))
    fmt.setOpacity(opacity)

    if halo_mm > 0 and badge_fill_hex is None:
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(halo_mm)
        buf.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        buf.setColor(QColor(halo_hex))
        buf.setOpacity(halo_opacity)
        fmt.setBuffer(buf)

    if badge_fill_hex is not None:
        bg = QgsTextBackgroundSettings()
        bg.setEnabled(True)
        bg.setType(QgsTextBackgroundSettings.ShapeRectangle)
        bg.setSizeType(QgsTextBackgroundSettings.SizeBuffer)
        bg.setSize(QSizeF(0.65, 0.35))
        bg.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        bg.setRadii(QSizeF(0.45, 0.45))
        bg.setRadiiUnit(QgsUnitTypes.RenderMillimeters)
        bg.setFillColor(QColor(badge_fill_hex))
        if badge_stroke_hex is not None:
            bg.setStrokeColor(QColor(badge_stroke_hex))
            bg.setStrokeWidth(0.22)
            bg.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
        fmt.setBackground(bg)

    return fmt


def _svg_marker_symbol(
    icon_name: str,
    size_mm: float,
    *,
    fill_hex: str = "#1A1A1A",
    outline_hex: str = "#1A1A1A",
    icons_root: Path | None = None,
) -> Any:
    """Create a crisp parameterized ``QgsMarkerSymbol`` backed by ``assets/icons/<icon>.svg``."""
    from qgis.core import QgsMarkerSymbol, QgsSvgMarkerSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtGui import QColor

    svg_path = get_icon_path(icon_name, icons_root=icons_root)
    if not svg_path.is_file():
        return QgsMarkerSymbol.createSimple(
            {
                "name": "circle",
                "color": fill_hex,
                "outline_color": "#FFFFFF",
                "outline_width": "0.25",
                "outline_width_unit": "MM",
                "size": f"{size_mm:.3f}",
                "size_unit": "MM",
            }
        )

    sym = QgsMarkerSymbol()
    sym.deleteSymbolLayer(0)
    svg_layer = QgsSvgMarkerSymbolLayer(str(svg_path), size_mm)
    svg_layer.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    svg_layer.setFillColor(QColor(fill_hex))
    svg_layer.setStrokeColor(QColor(outline_hex))
    sym.appendSymbolLayer(svg_layer)
    return sym


def _centroid_svg_fill_symbol(
    icon_name: str,
    size_mm: float,
    *,
    fill_hex: str = "#1A1A1A",
    outline_hex: str = "#1A1A1A",
    icons_root: Path | None = None,
) -> Any:
    """Place a crisp SVG usage icon at the interior centroid of a building polygon."""
    from qgis.core import QgsCentroidFillSymbolLayer, QgsFillSymbol

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)
    centroid = QgsCentroidFillSymbolLayer()
    centroid.setPointOnSurface(True)
    centroid.setSubSymbol(
        _svg_marker_symbol(
            icon_name,
            size_mm,
            fill_hex=fill_hex,
            outline_hex=outline_hex,
            icons_root=icons_root,
        )
    )
    sym.appendSymbolLayer(centroid)
    return sym


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
    bg_alpha: int = 255,
    outline_hex: str | None = None,
    outline_mm: float = 0.0,
    outline_dash: list[float] | None = None,
    dash_vector: list[float] | None = None,
) -> Any:
    """Build a crisp vector line-pattern hatch fill (ideal for B&W laser prints & FEMA)."""
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
    sym.deleteSymbolLayer(0)

    if bg_hex is not None:
        c = QColor(bg_hex)
        c.setAlpha(bg_alpha)
        bg = QgsSimpleFillSymbolLayer(c)
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
        if outline_dash:
            ol = QgsSimpleLineSymbolLayer(QColor(outline_hex), outline_mm)
            ol.setWidthUnit(QgsUnitTypes.RenderMillimeters)
            ol.setUseCustomDashPattern(True)
            ol.setCustomDashVector(outline_dash)
            ol.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
            sym.appendSymbolLayer(ol)
        else:
            outline = QgsSimpleFillSymbolLayer()
            outline.setBrushStyle(Qt.NoBrush)
            outline.setStrokeColor(QColor(outline_hex))
            outline.setStrokeWidth(outline_mm)
            outline.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
            sym.appendSymbolLayer(outline)

    return sym


def _svg_pattern_fill_symbol(
    icon_name: str,
    *,
    spacing_mm: float,
    icon_mm: float,
    icon_hex: str,
    bg_hex: str | None = None,
    hatch_hex: str | None = None,
    hatch_angle_deg: float = 60.0,
    hatch_spacing_mm: float = 2.8,
    hatch_mm: float = 0.10,
    icons_root: Path | None = None,
) -> Any:
    """Build a canopy pattern fill combining optional soft background/hatch + SVG tree icons."""
    from qgis.core import (
        QgsFillSymbol,
        QgsLinePatternFillSymbolLayer,
        QgsLineSymbol,
        QgsPointPatternFillSymbolLayer,
        QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer,
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

    if hatch_hex is not None:
        hpat = QgsLinePatternFillSymbolLayer()
        hpat.setLineAngle(hatch_angle_deg)
        hpat.setDistance(hatch_spacing_mm)
        hpat.setDistanceUnit(QgsUnitTypes.RenderMillimeters)
        sub_line = QgsLineSymbol()
        sub_line.deleteSymbolLayer(0)
        sl = QgsSimpleLineSymbolLayer(QColor(hatch_hex), hatch_mm)
        sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        sub_line.appendSymbolLayer(sl)
        hpat.setSubSymbol(sub_line)
        sym.appendSymbolLayer(hpat)

    pattern = QgsPointPatternFillSymbolLayer()
    pattern.setDistanceX(spacing_mm)
    pattern.setDistanceY(spacing_mm)
    pattern.setDistanceXUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDistanceYUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDisplacementX(spacing_mm * 0.5)
    pattern.setDisplacementXUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setSubSymbol(
        _svg_marker_symbol(
            icon_name,
            icon_mm,
            fill_hex=icon_hex,
            outline_hex=icon_hex,
            icons_root=icons_root,
        )
    )
    sym.appendSymbolLayer(pattern)
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


def _waterway_symbol(
    color_hex: str,
    width_mm: float,
    *,
    dash_vector: list[float] | None = None,
    show_arrows: bool = False,
    arrow_interval_mm: float = 30.0,
    arrow_size_mm: float = 1.9,
    arrow_color_hex: str | None = None,
    icons_root: Path | None = None,
) -> Any:
    """Waterway line symbol with optional downstream directional chevron arrows."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), width_mm)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.RoundCap)
    sl.setPenJoinStyle(Qt.RoundJoin)
    if dash_vector:
        sl.setUseCustomDashPattern(True)
        sl.setCustomDashVector(dash_vector)
        sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sym.appendSymbolLayer(sl)

    if show_arrows:
        arrows = QgsMarkerLineSymbolLayer(True, arrow_interval_mm)
        arrows.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
        if hasattr(arrows, "setOffsetAlongLine"):
            arrows.setOffsetAlongLine(arrow_interval_mm * 0.45)
            arrows.setOffsetAlongLineUnit(QgsUnitTypes.RenderMillimeters)
        ac = arrow_color_hex or color_hex
        arrows.setSubSymbol(
            _svg_marker_symbol(
                "river_arrow",
                arrow_size_mm,
                fill_hex=ac,
                outline_hex=ac,
                icons_root=icons_root,
            )
        )
        sym.appendSymbolLayer(arrows)

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
    centerline_dash: list[float] | None = None,
    centerline_pass: int = 28,
) -> Any:
    """Create a multi-layer road/runway symbol with explicit symbol-level passes."""
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
        if centerline_dash:
            center.setUseCustomDashPattern(True)
            center.setCustomDashVector(centerline_dash)
            center.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
        center.setRenderingPass(centerline_pass)
        sym.appendSymbolLayer(center)

    return sym


def _railway_symbol(color_hex: str = "#1A1A1A", base_pass: int = 15, wf: float = 1.0) -> Any:
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

    rail = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.30 * wf)
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
            "outline_width": f"{0.24 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{1.35 * wf:.3f}",
            "size_unit": "MM",
            "angle": "0",
        }
    )
    ticks.setSubSymbol(tick_marker)
    sym.appendSymbolLayer(ticks)
    return sym


def _powerline_symbol(color_hex: str, render_pass: int = 2, wf: float = 1.0) -> Any:
    """Military topo power transmission line: crisp thin line + perpendicular pylon ticks."""
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

    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.16 * wf)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.FlatCap)
    sl.setRenderingPass(render_pass)
    sym.appendSymbolLayer(sl)

    pylons = QgsMarkerLineSymbolLayer(True, 9.5)
    pylons.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    pylons.setRenderingPass(render_pass)
    pylon_marker = QgsMarkerSymbol.createSimple(
        {
            "name": "line",
            "color": color_hex,
            "outline_color": color_hex,
            "outline_width": f"{0.22 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{1.15 * wf:.3f}",
            "size_unit": "MM",
            "angle": "0",
        }
    )
    pylons.setSubSymbol(pylon_marker)
    sym.appendSymbolLayer(pylons)
    return sym


def _pipeline_symbol(color_hex: str, render_pass: int = 2, wf: float = 1.0) -> Any:
    """Distinct topo pipeline symbol: dash-dot line with hollow circle valve markers."""
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

    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.22 * wf)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.FlatCap)
    sl.setUseCustomDashPattern(True)
    sl.setCustomDashVector([3.0, 1.2])
    sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sl.setRenderingPass(render_pass)
    sym.appendSymbolLayer(sl)

    circles = QgsMarkerLineSymbolLayer(False, 8.4)
    circles.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    circles.setRenderingPass(render_pass)
    ring = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": "#FFFFFF",
            "outline_color": color_hex,
            "outline_width": f"{0.22 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{0.95 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    circles.setSubSymbol(ring)
    sym.appendSymbolLayer(circles)
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


def _build_landcover_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    from qgis.core import QgsRuleBasedRenderer

    root = QgsRuleBasedRenderer.Rule(None)
    is_color = color_mode == ColorMode.COLOR

    if carto.show_trees:
        evergreen_expr = (
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND (\"leaf_type\" = 'needleleaved' OR \"wood\" = 'coniferous' "
            "OR \"leaf_cycle\" = 'evergreen')"
        )
        deciduous_expr = (
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND (\"leaf_type\" = 'broadleaved' OR \"wood\" = 'deciduous' "
            "OR \"leaf_cycle\" = 'deciduous') "
            "AND NOT (\"leaf_type\" = 'needleleaved' OR \"wood\" = 'coniferous')"
        )
        mixed_forest_expr = (
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND NOT (\"leaf_type\" IN ('needleleaved','broadleaved') "
            "OR \"wood\" IN ('coniferous','deciduous') "
            "OR \"leaf_cycle\" IN ('evergreen','deciduous'))"
        )

        if is_color:
            _add_rule(
                root,
                "Evergreen / Coniferous Forest",
                evergreen_expr,
                _svg_pattern_fill_symbol(
                    "conifer",
                    spacing_mm=6.2,
                    icon_mm=1.6,
                    icon_hex="#4F7E46",
                    bg_hex="#BDD8B0",
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Deciduous / Broadleaf Forest",
                deciduous_expr,
                _svg_pattern_fill_symbol(
                    "deciduous",
                    spacing_mm=6.2,
                    icon_mm=1.6,
                    icon_hex="#688B48",
                    bg_hex="#D6E6B8",
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Mixed / Unspecified Forest",
                mixed_forest_expr,
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
        else:
            # Dedicated B&W laser-print canopy patterns: distinct evergreen vs deciduous vs mixed
            _add_rule(
                root,
                "Evergreen / Coniferous Forest",
                evergreen_expr,
                _svg_pattern_fill_symbol(
                    "conifer",
                    spacing_mm=5.6,
                    icon_mm=1.65,
                    icon_hex="#5E5E5E",
                    hatch_hex="#B0B0B0",
                    hatch_angle_deg=60.0,
                    hatch_spacing_mm=2.6,
                    hatch_mm=0.11,
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Deciduous / Broadleaf Forest",
                deciduous_expr,
                _svg_pattern_fill_symbol(
                    "deciduous",
                    spacing_mm=5.4,
                    icon_mm=1.65,
                    icon_hex="#5E5E5E",
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Mixed / Unspecified Forest",
                mixed_forest_expr,
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

    if is_color:
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


def _build_hillshade_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsBilinearRasterResampler, QgsSingleBandGrayRenderer
    from qgis.PyQt.QtGui import QPainter

    opacity, brightness, contrast = _hillshade_params(color_mode, carto.hillshade_strength)
    renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    renderer.setOpacity(opacity)
    layer.setRenderer(renderer)
    layer.setBlendMode(QPainter.CompositionMode_Multiply)

    if layer.resampleFilter() is not None:
        layer.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        layer.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())

    if layer.brightnessFilter() is not None:
        layer.brightnessFilter().setBrightness(brightness)
        layer.brightnessFilter().setContrast(contrast)


def _build_slope_shading_style(
    layer: Any,
    color_mode: ColorMode,
) -> None:
    """Style slope-angle avalanche shading raster (4-band RGBA or 1-band slope degrees).

    Lives on the lower map stack right above ``hillshade.tif`` with ``BlendNormal``
    so contours, trails, buildings, and the MGRS 1 km grid on the upper map stack
    remain 100% pure un-rasterized PDF vectors.
    """
    from qgis.core import (
        QgsBilinearRasterResampler,
        QgsColorRampShader,
        QgsMultiBandColorRenderer,
        QgsRasterShader,
        QgsSingleBandPseudoColorRenderer,
    )
    from qgis.PyQt.QtGui import QColor, QPainter

    is_color = color_mode == ColorMode.COLOR
    band_count = layer.bandCount() if hasattr(layer, "bandCount") else 4

    if band_count >= 3:
        renderer = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
        if band_count >= 4:
            renderer.setAlphaBand(4)
        # terrain.py RGBA already carries progressive band alpha (115..205 Color / 85..180 B&W);
        # scaling to 0.58 Color / 0.65 B&W yields crisp ~26-46% effective avalanche tint over hillshade.
        renderer.setOpacity(0.58 if is_color else 0.65)
        layer.setRenderer(renderer)
    else:
        shader = QgsRasterShader()
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Discrete)
        if is_color:
            items = [
                QgsColorRampShader.ColorRampItem(26.999, QColor(0, 0, 0, 0), "< 27°"),
                QgsColorRampShader.ColorRampItem(29.999, QColor(246, 224, 94, 195), "27°–30°"),
                QgsColorRampShader.ColorRampItem(34.999, QColor(237, 137, 54, 215), "30°–35°"),
                QgsColorRampShader.ColorRampItem(44.999, QColor(229, 62, 62, 230), "35°–45°"),
                QgsColorRampShader.ColorRampItem(49.999, QColor(128, 90, 213, 235), "45°–50°"),
                QgsColorRampShader.ColorRampItem(90.0, QColor(59, 7, 100, 240), ">= 50°"),
            ]
        else:
            items = [
                QgsColorRampShader.ColorRampItem(29.999, QColor(0, 0, 0, 0), "< 30°"),
                QgsColorRampShader.ColorRampItem(34.999, QColor(120, 120, 120, 150), "30°–35°"),
                QgsColorRampShader.ColorRampItem(44.999, QColor(55, 55, 55, 195), "35°–45°"),
                QgsColorRampShader.ColorRampItem(90.0, QColor(15, 15, 15, 230), ">= 45°"),
            ]
        ramp.setColorRampItemList(items)
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setOpacity(0.42 if is_color else 0.35)
        layer.setRenderer(renderer)

    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    if layer.resampleFilter() is not None:
        layer.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        layer.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())


def _build_viewshed_style(
    layer: Any,
    color_mode: ColorMode,
) -> None:
    """Style RF / line-of-sight viewshed coverage raster on the lower raster stack.

    Supports both 4-band RGBA Byte GeoTIFFs (from ``terrain.build_viewshed_products``)
    and 1-band binary visibility rasters (0/255), keeping ``BlendNormal`` at 0.52 Color /
    0.55 B&W opacity right above ``hillshade.tif`` so all upper-stack vectors remain pure.
    """
    from qgis.core import (
        QgsBilinearRasterResampler,
        QgsColorRampShader,
        QgsMultiBandColorRenderer,
        QgsRasterShader,
        QgsSingleBandPseudoColorRenderer,
    )
    from qgis.PyQt.QtGui import QColor, QPainter

    is_color = color_mode == ColorMode.COLOR
    band_count = layer.bandCount() if hasattr(layer, "bandCount") else 4

    if band_count >= 3:
        renderer = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
        if band_count >= 4:
            renderer.setAlphaBand(4)
        renderer.setOpacity(0.52 if is_color else 0.55)
        layer.setRenderer(renderer)
    else:
        shader = QgsRasterShader()
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Discrete)
        if is_color:
            items = [
                QgsColorRampShader.ColorRampItem(0.5, QColor(0, 0, 0, 0), "Shadowed"),
                QgsColorRampShader.ColorRampItem(255.0, QColor(0, 212, 146, 170), "Visible"),
            ]
        else:
            items = [
                QgsColorRampShader.ColorRampItem(0.5, QColor(0, 0, 0, 0), "Shadowed"),
                QgsColorRampShader.ColorRampItem(255.0, QColor(80, 80, 80, 145), "Visible"),
            ]
        ramp.setColorRampItemList(items)
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setOpacity(0.52 if is_color else 0.55)
        layer.setRenderer(renderer)

    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    if layer.resampleFilter() is not None:
        layer.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        layer.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())


def _build_fema_flood_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style FEMA NFHL S_Fld_Haz_Ar polygons (SFHA 100-yr + moderate 500-yr flood zones)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    is_color = color_mode == ColorMode.COLOR
    sfha_expr = (
        "upper(coalesce(\"sfha_tf\", '')) = 'T' "
        "OR upper(coalesce(\"fld_zone\", '')) LIKE 'A%' "
        "OR upper(coalesce(\"fld_zone\", '')) LIKE 'V%'"
    )
    mod_expr = (
        f"NOT ({sfha_expr}) AND ("
        "upper(coalesce(\"zone_subty\", '')) LIKE '%0.2%' "
        "OR upper(coalesce(\"fld_zone\", '')) IN ('B','X500')"
        ")"
    )

    root = QgsRuleBasedRenderer.Rule(None)
    if is_color:
        _add_rule(
            root,
            "FEMA SFHA (100-yr Flood Zone)",
            sfha_expr,
            _hatch_fill_symbol(
                "#1976D2",
                angle_deg=45.0,
                spacing_mm=2.2,
                line_mm=0.15,
                bg_hex="#1E88E5",
                bg_alpha=34,
                outline_hex="#1565C0",
                outline_mm=0.18,
            ),
        )
        _add_rule(
            root,
            "FEMA 500-yr Flood Zone",
            mod_expr,
            _hatch_fill_symbol(
                "#64B5F6",
                angle_deg=135.0,
                spacing_mm=3.4,
                line_mm=0.12,
                dash_vector=[1.4, 1.2],
                outline_hex="#42A5F5",
                outline_mm=0.14,
                outline_dash=[1.4, 1.0],
            ),
        )
    else:
        _add_rule(
            root,
            "FEMA SFHA (100-yr Flood Zone)",
            sfha_expr,
            _hatch_fill_symbol(
                "#444444",
                angle_deg=45.0,
                spacing_mm=2.2,
                line_mm=0.15,
                dash_vector=[0.8, 1.0],
                outline_hex="#222222",
                outline_mm=0.18,
                outline_dash=[1.6, 1.0],
            ),
        )
        _add_rule(
            root,
            "FEMA 500-yr Flood Zone",
            mod_expr,
            _hatch_fill_symbol(
                "#777777",
                angle_deg=135.0,
                spacing_mm=3.5,
                line_mm=0.12,
                dash_vector=[0.6, 1.4],
            ),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    fd = scale_font_delta(scale)
    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "'ZONE ' || \"fld_zone\""
    pal.isExpression = True
    pal.priority = 2
    pal.setFormat(
        _make_text_format(
            5.6 + fd,
            "#0D47A1" if is_color else "#1A1A1A",
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.6,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("FEMA Zone Code")
    rule.setFilterExpression(f"({sfha_expr}) AND \"fld_zone\" IS NOT NULL AND length(\"fld_zone\") > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_water_polygons_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    water_filter = (
        "\"natural\" = 'water' OR \"water\" IS NOT NULL "
        "OR \"waterway\" IN ('riverbank','dock','canal') "
        "OR \"landuse\" IN ('reservoir','basin')"
    )
    root = QgsRuleBasedRenderer.Rule(None)
    if color_mode == ColorMode.COLOR:
        sym = _simple_fill_symbol("#A6D1EC", stroke_hex="#3A82B5", stroke_mm=0.20 * wf, stroke_style="solid")
    else:
        sym = _hatch_fill_symbol(
            "#6B6B6B",
            angle_deg=0.0,
            spacing_mm=1.25,
            line_mm=0.12,
            bg_hex="#FFFFFF",
            outline_hex="#1A1A1A",
            outline_mm=0.26 * wf,
        )
    _add_rule(root, "Waterbodies", water_filter, sym)
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 5
    pal.setFormat(
        _make_text_format(
            6.5 + fd,
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
    layer.setLabelsEnabled(carto.label_density != DensityLevel.LOW)


def _build_contours_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)

    root = QgsRuleBasedRenderer.Rule(None)
    if color_mode == ColorMode.COLOR:
        idx_color, minor_color = "#8F5B28", "#A87846"
        idx_w, minor_w = 0.32 * wf, 0.15 * wf
        label_color, halo_color = "#78481B", "#FAF9F5"
    else:
        idx_color, minor_color = "#1A1A1A", "#505050"
        idx_w, minor_w = 0.30 * wf, 0.14 * wf
        label_color, halo_color = "#111111", "#FFFFFF"

    _add_rule(root, "Index Contours", "\"is_index\" = 1", _line_symbol(idx_color, idx_w))
    _add_rule(
        root,
        "Intermediate Contours",
        "\"is_index\" = 0 OR \"is_index\" IS NULL",
        _line_symbol(minor_color, minor_w),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "elev_label"
    pal.isExpression = False
    pal.priority = 3
    _configure_curved_line_pal(
        pal,
        on_line=True,
        repeat_mm=55.0 * rep_mult,
        min_feature_mm=12.0 * min_mult,
    )
    pal.setFormat(
        _make_text_format(
            5.8 + fd,
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


def _build_enc_depth_contours_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style NOAA ENC depth contours (``enc_depth_contours`` with ``valdco``)."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    is_color = color_mode == ColorMode.COLOR
    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    c_hex = "#1565C0" if is_color else "#222222"

    layer.setRenderer(QgsSingleSymbolRenderer(_line_symbol(c_hex, 0.22 * wf, dash_vector=[2.2, 1.0])))

    pal = QgsPalLayerSettings()
    pal.fieldName = "CASE WHEN \"valdco\" IS NOT NULL THEN round(to_real(\"valdco\"), 1) || ' m' ELSE '' END"
    pal.isExpression = True
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=True, repeat_mm=60.0 * rep_mult, min_feature_mm=10.0 * min_mult)
    pal.setFormat(_make_text_format(5.5 + fd, c_hex, italic=True, halo_hex="#FFFFFF", halo_mm=0.55))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_nhd_flowlines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style USGS NHD flowlines (perennial, intermittent, ephemeral, canal)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    is_color = color_mode == ColorMode.COLOR
    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    arrow_iv = 34.0 / wf

    perennial_c = "#2474B5" if is_color else "#1A1A1A"
    intermit_c = "#358AC9" if is_color else "#333333"
    ephem_c = "#5C9FD4" if is_color else "#555555"
    halo_c = "#FAF9F5" if is_color else "#FFFFFF"

    root = QgsRuleBasedRenderer.Rule(None)
    _add_rule(
        root,
        "NHD Perennial Stream",
        "\"flow_type\" = 'perennial'",
        _waterway_symbol(
            perennial_c,
            0.28 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=1.7 * wf,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "NHD Intermittent Stream",
        "\"flow_type\" = 'intermittent'",
        _waterway_symbol(
            intermit_c,
            0.22 * wf,
            dash_vector=[2.6, 0.9, 0.5, 0.9],
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv * 1.25,
            arrow_size_mm=1.5 * wf,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "NHD Ephemeral Stream",
        "\"flow_type\" = 'ephemeral'",
        _line_symbol(ephem_c, 0.18 * wf, dash_vector=[0.8, 1.0]),
    )
    _add_rule(
        root,
        "NHD Canal / Other",
        "\"flow_type\" IS NULL OR \"flow_type\" NOT IN ('perennial','intermittent','ephemeral')",
        _waterway_symbol(
            perennial_c,
            0.26 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=1.6 * wf,
            icons_root=icons_root,
        ),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "gnis_name"
    pal.isExpression = False
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=70.0 * rep_mult, min_feature_mm=15.0 * min_mult)
    pal.setFormat(_make_text_format(5.8 + fd, perennial_c, italic=True, halo_hex=halo_c, halo_mm=0.55))
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("NHD Stream Names")
    rule.setFilterExpression("\"gnis_name\" IS NOT NULL AND length(\"gnis_name\") > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_waterways_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    arrow_iv = 30.0 / wf

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
        _waterway_symbol(
            river_c,
            0.55 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=2.1 * wf,
            arrow_color_hex=label_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "Streams & Creeks",
        "\"waterway\" IN ('stream','creek','drain','ditch') AND (\"intermittent\" IS NULL OR \"intermittent\" != 'yes')",
        _waterway_symbol(
            stream_c,
            0.24 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv * 1.15,
            arrow_size_mm=1.65 * wf,
            arrow_color_hex=label_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "Intermittent Waterways",
        "\"waterway\" IN ('river','stream','creek','canal','drain','ditch') AND \"intermittent\" = 'yes'",
        _line_symbol(stream_c, 0.22 * wf, dash_vector=[2.8, 0.9, 0.5, 0.9]),
    )
    _add_rule(
        root,
        "Dams & Weirs",
        "\"waterway\" IN ('dam','weir','waterfall')",
        _line_symbol(river_c, 0.45 * wf, cap_round=False),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=75.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    pal.setFormat(_make_text_format(6.0 + fd, label_c, italic=True, halo_hex=halo_c, halo_mm=0.55))
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Waterway Names")
    rule.setFilterExpression(
        "\"waterway\" IN ('river','stream','creek','canal') AND \"name\" IS NOT NULL AND length(\"name\") > 0"
    )
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_buildings_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsRuleBasedRenderer

    root = QgsRuleBasedRenderer.Rule(None)
    is_color = color_mode == ColorMode.COLOR

    if carto.show_buildings:
        if is_color:
            sym = _simple_fill_symbol("#6E6E6E", stroke_hex="#404040", stroke_mm=0.10, stroke_style="solid")
        else:
            sym = _simple_fill_symbol("#262626", stroke_hex="#000000", stroke_mm=0.10, stroke_style="solid")
        _add_rule(root, "Buildings", "\"building\" IS NOT NULL AND \"building\" != 'no'", sym)

    if carto.show_power:
        sub_sym = _hatch_fill_symbol(
            "#555555" if is_color else "#222222",
            angle_deg=135.0,
            spacing_mm=1.6,
            line_mm=0.12,
            bg_hex="#E6E2DC" if is_color else "#F2F2F2",
            outline_hex="#333333" if is_color else "#000000",
            outline_mm=0.22,
        )
        _add_rule(root, "Power Substations", "\"power\" = 'substation'", sub_sym)

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLabelsEnabled(False)


def _build_building_icons_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Render military/topo usage icons at the interior centroid of tagged OSM building polygons."""
    from qgis.core import QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    is_color = color_mode == ColorMode.COLOR
    ink = "#1A1A1A" if is_color else "#000000"
    red = "#C62828" if is_color else "#000000"
    blue = "#1B4F72" if is_color else "#000000"

    root = QgsRuleBasedRenderer.Rule(None)
    if carto.show_buildings:
        _add_rule(
            root,
            "Hospital Buildings",
            "\"amenity\" IN ('hospital','clinic') OR \"building\" = 'hospital' OR \"emergency\" = 'ambulance_station'",
            _centroid_svg_fill_symbol("hospital", 2.7 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fire Station Buildings",
            "\"amenity\" = 'fire_station' OR \"emergency\" = 'fire_station'",
            _centroid_svg_fill_symbol("fire_station", 2.6 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Police Buildings",
            "\"amenity\" = 'police'",
            _centroid_svg_fill_symbol("police", 2.6 * wf, fill_hex=blue, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "School / University Buildings",
            "\"amenity\" IN ('school','college','university') OR \"building\" = 'school'",
            _centroid_svg_fill_symbol("school", 2.5 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Place of Worship Buildings",
            "\"amenity\" = 'place_of_worship' OR \"building\" IN ('church','chapel','cathedral','mosque','temple','synagogue')",
            _centroid_svg_fill_symbol("worship", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fuel Station Buildings",
            "\"amenity\" = 'fuel'",
            _centroid_svg_fill_symbol("fuel", 2.3 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Shelter / Hut Buildings",
            "\"amenity\" = 'shelter' OR \"building\" = 'hut'",
            _centroid_svg_fill_symbol("shelter", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )

    _add_rule(
        root,
        "Helipad Polygons",
        "\"aeroway\" = 'helipad'",
        _centroid_svg_fill_symbol("helipad", 2.8 * wf, fill_hex=blue, outline_hex=blue, icons_root=icons_root),
    )

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLabelsEnabled(False)


def _build_transport_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    is_color = color_mode == ColorMode.COLOR

    root = QgsRuleBasedRenderer.Rule(None)
    if is_color:
        _add_rule(
            root,
            "Motorway / Trunk",
            "\"highway\" IN ('motorway','motorway_link','trunk','trunk_link')",
            _cased_road_symbol(
                "#5E1D12",
                1.40 * wf,
                "#C84B31",
                0.95 * wf,
                casing_pass=12,
                fill_pass=24,
                centerline_hex="#FAF9F5",
                centerline_mm=0.14 * wf,
                centerline_pass=26,
            ),
        )
        _add_rule(
            root,
            "Primary Road",
            "\"highway\" IN ('primary','primary_link')",
            _cased_road_symbol("#6E3A0C", 1.15 * wf, "#D97925", 0.75 * wf, casing_pass=10, fill_pass=22),
        )
        _add_rule(
            root,
            "Secondary Road",
            "\"highway\" IN ('secondary','secondary_link')",
            _cased_road_symbol("#6E5614", 0.95 * wf, "#E5B742", 0.60 * wf, casing_pass=8, fill_pass=20),
        )
        _add_rule(
            root,
            "Tertiary / Residential",
            "\"highway\" IN ('tertiary','tertiary_link','unclassified','residential','living_street','road')",
            _cased_road_symbol("#4A4A4A", 0.72 * wf, "#FFFFFF", 0.44 * wf, casing_pass=6, fill_pass=18),
        )
        _add_rule(
            root,
            "Service Road",
            "\"highway\" = 'service'",
            _cased_road_symbol("#686868", 0.46 * wf, "#F7F7F7", 0.26 * wf, casing_pass=4, fill_pass=16),
        )
        _add_rule(
            root,
            "Unpaved Track / 4WD",
            "\"highway\" = 'track'",
            _line_symbol("#5E442B", 0.34 * wf, dash_vector=[2.2, 1.0], render_pass=5),
        )
        _add_rule(
            root,
            "Trails / Footways / Paths",
            "\"highway\" IN ('path','footway','bridleway','cycleway','steps')",
            _line_symbol("#3D3D3D", 0.24 * wf, dash_vector=[1.1, 0.9], render_pass=3),
        )
        _add_rule(
            root,
            "Railway",
            "\"railway\" IN ('rail','light_rail','narrow_gauge','preserved','tram')",
            _railway_symbol("#1A1A1A", base_pass=15, wf=wf),
        )
        _add_rule(
            root,
            "Runways",
            "\"aeroway\" = 'runway'",
            _cased_road_symbol(
                "#1A1A1A",
                1.65 * wf,
                "#666666",
                1.20 * wf,
                casing_pass=9,
                fill_pass=21,
                centerline_hex="#FFFFFF",
                centerline_mm=0.20 * wf,
                centerline_dash=[1.8, 1.2],
                centerline_pass=27,
            ),
        )
        _add_rule(
            root,
            "Taxiways",
            "\"aeroway\" = 'taxiway'",
            _cased_road_symbol("#333333", 0.85 * wf, "#B0B0B0", 0.55 * wf, casing_pass=7, fill_pass=19),
        )
        if carto.show_power:
            _add_rule(
                root,
                "Power Lines",
                "\"power\" IN ('line','minor_line')",
                _powerline_symbol("#555555", render_pass=2, wf=wf),
            )
        if carto.show_pipelines:
            _add_rule(
                root,
                "Pipelines",
                "\"man_made\" = 'pipeline'",
                _pipeline_symbol("#6A4C93", render_pass=2, wf=wf),
            )
    else:
        _add_rule(
            root,
            "Motorway / Trunk",
            "\"highway\" IN ('motorway','motorway_link','trunk','trunk_link')",
            _cased_road_symbol(
                "#000000",
                1.45 * wf,
                "#FFFFFF",
                0.88 * wf,
                casing_pass=12,
                fill_pass=24,
                centerline_hex="#000000",
                centerline_mm=0.22 * wf,
                centerline_pass=26,
            ),
        )
        _add_rule(
            root,
            "Primary Road",
            "\"highway\" IN ('primary','primary_link')",
            _cased_road_symbol("#000000", 1.15 * wf, "#C8C8C8", 0.68 * wf, casing_pass=10, fill_pass=22),
        )
        _add_rule(
            root,
            "Secondary Road",
            "\"highway\" IN ('secondary','secondary_link')",
            _cased_road_symbol("#000000", 0.95 * wf, "#FFFFFF", 0.56 * wf, casing_pass=8, fill_pass=20),
        )
        _add_rule(
            root,
            "Tertiary / Residential",
            "\"highway\" IN ('tertiary','tertiary_link','unclassified','residential','living_street','road')",
            _cased_road_symbol("#222222", 0.68 * wf, "#FFFFFF", 0.40 * wf, casing_pass=6, fill_pass=18),
        )
        _add_rule(
            root,
            "Service Road",
            "\"highway\" = 'service'",
            _line_symbol("#444444", 0.28 * wf, render_pass=4),
        )
        _add_rule(
            root,
            "Unpaved Track / 4WD",
            "\"highway\" = 'track'",
            _line_symbol("#1A1A1A", 0.34 * wf, dash_vector=[2.2, 1.0], render_pass=5),
        )
        _add_rule(
            root,
            "Trails / Footways / Paths",
            "\"highway\" IN ('path','footway','bridleway','cycleway','steps')",
            _line_symbol("#1A1A1A", 0.24 * wf, dash_vector=[0.85, 0.85], render_pass=3),
        )
        _add_rule(
            root,
            "Railway",
            "\"railway\" IN ('rail','light_rail','narrow_gauge','preserved','tram')",
            _railway_symbol("#000000", base_pass=15, wf=wf),
        )
        _add_rule(
            root,
            "Runways",
            "\"aeroway\" = 'runway'",
            _cased_road_symbol(
                "#000000",
                1.65 * wf,
                "#444444",
                1.20 * wf,
                casing_pass=9,
                fill_pass=21,
                centerline_hex="#FFFFFF",
                centerline_mm=0.22 * wf,
                centerline_dash=[1.8, 1.2],
                centerline_pass=27,
            ),
        )
        _add_rule(
            root,
            "Taxiways",
            "\"aeroway\" = 'taxiway'",
            _cased_road_symbol("#000000", 0.85 * wf, "#BDBDBD", 0.55 * wf, casing_pass=7, fill_pass=19),
        )
        if carto.show_power:
            _add_rule(
                root,
                "Power Lines",
                "\"power\" IN ('line','minor_line')",
                _powerline_symbol("#333333", render_pass=2, wf=wf),
            )
        if carto.show_pipelines:
            _add_rule(
                root,
                "Pipelines",
                "\"man_made\" = 'pipeline'",
                _pipeline_symbol("#1A1A1A", render_pass=2, wf=wf),
            )

    renderer = QgsRuleBasedRenderer(root)
    renderer.setUsingSymbolLevels(True)
    layer.setRenderer(renderer)

    # Hierarchical road labeling (shield badge for highway ref + curved street names)
    halo_hex = "#FAF9F5" if is_color else "#FFFFFF"
    label_root = QgsRuleBasedLabeling.Rule(None)

    # Highway route shield badge on motorway/trunk/primary with ref
    pal_shield = QgsPalLayerSettings()
    pal_shield.fieldName = "trim(array_first(string_to_array(\"ref\", ';')))"
    pal_shield.isExpression = True
    pal_shield.priority = 8
    _configure_curved_line_pal(pal_shield, on_line=True, repeat_mm=80.0 * rep_mult, min_feature_mm=18.0 * min_mult)
    pal_shield.setFormat(
        _make_text_format(
            5.8 + fd,
            "#111111",
            bold=True,
            badge_fill_hex="#FFFFFF",
            badge_stroke_hex="#5E1D12" if is_color else "#000000",
        )
    )
    r_shield = QgsRuleBasedLabeling.Rule(pal_shield)
    r_shield.setDescription("Highway Route Shields")
    r_shield.setFilterExpression(
        "\"highway\" IN ('motorway','trunk','primary') AND \"ref\" IS NOT NULL AND length(\"ref\") > 0"
    )
    label_root.appendChild(r_shield)

    pal_major = QgsPalLayerSettings()
    pal_major.fieldName = (
        "CASE WHEN \"name\" IS NOT NULL AND length(\"name\") > 0 THEN \"name\" "
        "ELSE \"ref\" END"
    )
    pal_major.isExpression = True
    pal_major.priority = 7
    _configure_curved_line_pal(pal_major, on_line=False, repeat_mm=65.0 * rep_mult, min_feature_mm=14.0 * min_mult)
    pal_major.setFormat(_make_text_format(6.4 + fd, "#1A1A1A", bold=True, halo_hex=halo_hex, halo_mm=0.7))
    r_major = QgsRuleBasedLabeling.Rule(pal_major)
    r_major.setDescription("Major Road Names")
    r_major.setFilterExpression(
        "\"highway\" IN ('motorway','trunk','primary','secondary') "
        "AND ((\"name\" IS NOT NULL AND length(\"name\") > 0) OR (\"ref\" IS NOT NULL AND length(\"ref\") > 0))"
    )
    label_root.appendChild(r_major)

    if carto.label_density != DensityLevel.LOW:
        minor_highways = (
            "('tertiary','unclassified','residential','service','track','path','footway')"
            if carto.label_density == DensityLevel.HIGH
            else "('tertiary','unclassified','residential','track','path','footway')"
        )
        pal_minor = QgsPalLayerSettings()
        pal_minor.fieldName = "name"
        pal_minor.isExpression = False
        pal_minor.priority = 5
        _configure_curved_line_pal(
            pal_minor,
            on_line=False,
            repeat_mm=70.0 * rep_mult,
            min_feature_mm=15.0 * min_mult,
        )
        pal_minor.setFormat(
            _make_text_format(5.6 + fd, "#2A2A2A" if is_color else "#111111", halo_hex=halo_hex, halo_mm=0.6)
        )
        r_minor = QgsRuleBasedLabeling.Rule(pal_minor)
        r_minor.setDescription("Minor Road & Trail Labels")
        r_minor.setFilterExpression(
            f"\"highway\" IN {minor_highways} AND \"name\" IS NOT NULL AND length(\"name\") > 0"
        )
        label_root.appendChild(r_minor)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_points_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    halo_hex = "#FAF9F5" if is_color else "#FFFFFF"
    ink = "#1A1A1A" if is_color else "#000000"
    red = "#C62828" if is_color else "#000000"
    blue = "#1B4F72" if is_color else "#000000"

    root = QgsRuleBasedRenderer.Rule(None)

    # Peaks & Volcanoes
    peak_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "triangle",
            "color": "#111111",
            "outline_color": "#FFFFFF",
            "outline_width": "0.28",
            "outline_width_unit": "MM",
            "size": f"{2.3 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Peaks & Summits", "\"natural\" IN ('peak','volcano')", peak_sym)

    # Saddles / Mountain Passes
    _add_rule(
        root,
        "Saddles & Passes",
        "\"natural\" = 'saddle'",
        _svg_marker_symbol("saddle", 2.3 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
    )

    # Highway Exits / Motorway Junctions
    exit_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#FFFFFF",
            "outline_color": "#C84B31" if is_color else "#000000",
            "outline_width": "0.42",
            "outline_width_unit": "MM",
            "size": f"{1.9 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Highway Exits", "\"highway\" = 'motorway_junction'", exit_sym)

    # Helipads & Aerodromes
    _add_rule(
        root,
        "Helipads",
        "\"aeroway\" = 'helipad'",
        _svg_marker_symbol("helipad", 2.8 * wf, fill_hex=blue, outline_hex=blue, icons_root=icons_root),
    )
    aero_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": "#FFFFFF",
            "outline_color": "#111111",
            "outline_width": "0.35",
            "outline_width_unit": "MM",
            "size": f"{2.2 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Aerodromes", "\"aeroway\" = 'aerodrome'", aero_sym)

    # Building / POI Usage SVG Icons
    if carto.show_buildings:
        _add_rule(
            root,
            "Hospitals",
            "\"amenity\" IN ('hospital','clinic') OR \"emergency\" = 'ambulance_station' OR \"building\" = 'hospital'",
            _svg_marker_symbol("hospital", 2.7 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fire Stations",
            "\"amenity\" = 'fire_station' OR \"emergency\" = 'fire_station'",
            _svg_marker_symbol("fire_station", 2.6 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Police Stations",
            "\"amenity\" = 'police'",
            _svg_marker_symbol("police", 2.6 * wf, fill_hex=blue, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Schools & Universities",
            "\"amenity\" IN ('school','college','university') OR \"building\" = 'school'",
            _svg_marker_symbol("school", 2.5 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Places of Worship",
            "\"amenity\" = 'place_of_worship' OR \"building\" IN ('church','chapel','cathedral','mosque','temple','synagogue')",
            _svg_marker_symbol("worship", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fuel Stations",
            "\"amenity\" = 'fuel'",
            _svg_marker_symbol("fuel", 2.3 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Shelters & Huts",
            "\"amenity\" = 'shelter' OR \"tourism\" IN ('alpine_hut','wilderness_hut','camp_site') OR \"building\" = 'hut'",
            _svg_marker_symbol("shelter", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )

    # Communications Towers & Masts
    _add_rule(
        root,
        "Communications Towers & Masts",
        "\"man_made\" IN ('tower','mast','communications_tower','water_tower','lighthouse') "
        "OR \"tower:type\" IN ('communication','radio','radar')",
        _svg_marker_symbol("comm_tower", 2.5 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
    )

    # Power Towers (at detailed scales <= 1:25k when show_power is enabled)
    if carto.show_power and scale <= 25000:
        pylon_sym = QgsMarkerSymbol.createSimple(
            {
                "name": "cross2",
                "color": "#444444" if is_color else "#1A1A1A",
                "outline_color": "#444444" if is_color else "#1A1A1A",
                "outline_width": "0.22",
                "outline_width_unit": "MM",
                "size": f"{1.1 * wf:.3f}",
                "size_unit": "MM",
            }
        )
        _add_rule(root, "Power Towers", "\"power\" = 'tower'", pylon_sym)

    # Viewpoints & Railway Stations
    vp_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "diamond",
            "color": "#333333" if is_color else "#111111",
            "outline_color": "#FFFFFF",
            "outline_width": "0.22",
            "outline_width_unit": "MM",
            "size": f"{1.5 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Viewpoints", "\"tourism\" = 'viewpoint'", vp_sym)

    station_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#1A1A1A",
            "outline_color": "#FFFFFF",
            "outline_width": "0.25",
            "outline_width_unit": "MM",
            "size": f"{1.6 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Railway Stations", "\"railway\" IN ('station','halt')", station_sym)

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
    pal_town.setFormat(_make_text_format(9.0 + fd, "#111111", bold=True, halo_hex=halo_hex, halo_mm=0.95))
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
    pal_village.setFormat(_make_text_format(7.6 + fd, "#1E1E1E", bold=True, halo_hex=halo_hex, halo_mm=0.85))
    r_village = QgsRuleBasedLabeling.Rule(pal_village)
    r_village.setDescription("Villages & Suburbs")
    r_village.setFilterExpression("\"place\" IN ('village','suburb') AND \"name\" IS NOT NULL")
    label_root.appendChild(r_village)

    # 3. Peaks (name + deduplicated elevation in metric or imperial)
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
    pal_peak.setFormat(_make_text_format(6.8 + fd, "#111111", bold=True, halo_hex=halo_hex, halo_mm=0.75))
    r_peak = QgsRuleBasedLabeling.Rule(pal_peak)
    r_peak.setDescription("Peaks & Elevations")
    r_peak.setFilterExpression("\"natural\" IN ('peak','volcano') AND (\"name\" IS NOT NULL OR \"ele\" IS NOT NULL)")
    label_root.appendChild(r_peak)

    # 4. Highway Exit Numbers
    pal_exit = QgsPalLayerSettings()
    pal_exit.fieldName = (
        "CASE WHEN \"ref\" IS NOT NULL AND length(\"ref\") > 0 THEN 'Exit ' || \"ref\" "
        "WHEN \"name\" IS NOT NULL THEN \"name\" ELSE '' END"
    )
    pal_exit.isExpression = True
    pal_exit.priority = 8
    _configure_point_pal(pal_exit, over_point=False, dist_mm=1.4)
    pal_exit.setFormat(
        _make_text_format(
            6.0 + fd,
            "#8C1D18" if is_color else "#000000",
            bold=True,
            badge_fill_hex="#FFFFFF",
            badge_stroke_hex="#C84B31" if is_color else "#000000",
        )
    )
    r_exit = QgsRuleBasedLabeling.Rule(pal_exit)
    r_exit.setDescription("Highway Exits")
    r_exit.setFilterExpression(
        "\"highway\" = 'motorway_junction' AND (\"ref\" IS NOT NULL OR \"name\" IS NOT NULL)"
    )
    label_root.appendChild(r_exit)

    # 5. Localities, Viewpoints, Stations, Saddles (suppressed at LOW label density)
    if carto.label_density != DensityLevel.LOW:
        pal_local = QgsPalLayerSettings()
        pal_local.fieldName = "name"
        pal_local.isExpression = False
        pal_local.priority = 6
        _configure_point_pal(pal_local, over_point=False, dist_mm=1.3)
        pal_local.setFormat(
            _make_text_format(6.4 + fd, "#2B2B2B" if is_color else "#111111", halo_hex=halo_hex, halo_mm=0.65)
        )
        r_local = QgsRuleBasedLabeling.Rule(pal_local)
        r_local.setDescription("Localities / Viewpoints / Stations / Saddles")
        r_local.setFilterExpression(
            "(\"place\" IN ('hamlet','locality') OR \"tourism\" IN ('viewpoint','alpine_hut','wilderness_hut') "
            "OR \"railway\" IN ('station','halt') OR \"natural\" = 'saddle') "
            "AND \"name\" IS NOT NULL"
        )
        label_root.appendChild(r_local)

    # 6. Minor POI names at HIGH label density
    if carto.label_density == DensityLevel.HIGH and carto.show_buildings:
        pal_poi = QgsPalLayerSettings()
        pal_poi.fieldName = "name"
        pal_poi.isExpression = False
        pal_poi.priority = 4
        _configure_point_pal(pal_poi, over_point=False, dist_mm=1.6)
        pal_poi.setFormat(
            _make_text_format(5.6 + fd, "#333333" if is_color else "#111111", halo_hex=halo_hex, halo_mm=0.55)
        )
        r_poi = QgsRuleBasedLabeling.Rule(pal_poi)
        r_poi.setDescription("POI & Facility Names")
        r_poi.setFilterExpression(
            "\"amenity\" IN ('hospital','fire_station','police','school','shelter','fuel') "
            "AND \"name\" IS NOT NULL"
        )
        label_root.appendChild(r_poi)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_us_towers_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style FAA DOF / FCC ASR tall obstacle towers (``us_towers``)."""
    from qgis.core import (
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#1B4F72" if is_color else "#000000"

    sym = _svg_marker_symbol("obstacle_tower", 3.0 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    if units == Units.IMPERIAL:
        lbl_expr = (
            "CASE "
            "WHEN \"agl_ft\" IS NOT NULL AND to_real(\"agl_ft\") > 0 "
            "THEN to_int(round(to_real(\"agl_ft\"))) || ' ft AGL' "
            "WHEN \"amsl_ft\" IS NOT NULL AND to_real(\"amsl_ft\") > 0 "
            "THEN to_int(round(to_real(\"amsl_ft\"))) || ' ft MSL' "
            "WHEN \"name\" IS NOT NULL THEN \"name\" "
            "ELSE '' END"
        )
    else:
        lbl_expr = (
            "CASE "
            "WHEN \"agl_ft\" IS NOT NULL AND to_real(\"agl_ft\") > 0 "
            "THEN to_int(round(to_real(\"agl_ft\") * 0.3048)) || ' m AGL' "
            "WHEN \"amsl_ft\" IS NOT NULL AND to_real(\"amsl_ft\") > 0 "
            "THEN to_int(round(to_real(\"amsl_ft\") * 0.3048)) || ' m MSL' "
            "WHEN \"name\" IS NOT NULL THEN \"name\" "
            "ELSE '' END"
        )

    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 7
    _configure_point_pal(pal, over_point=False, dist_mm=1.7)
    pal.setFormat(_make_text_format(5.8 + fd, "#10324A" if is_color else "#000000", bold=True, halo_hex="#FFFFFF", halo_mm=0.65))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_enc_navaids_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style NOAA ENC buoys, beacons, and lights (``enc_navaids``)."""
    from qgis.core import (
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#1B4F72" if is_color else "#000000"

    sym = _svg_marker_symbol("navaid", 2.6 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    pal = QgsPalLayerSettings()
    pal.fieldName = "objnam"
    pal.isExpression = False
    pal.priority = 6
    _configure_point_pal(pal, over_point=False, dist_mm=1.5)
    pal.setFormat(_make_text_format(5.8 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.6))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
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


def _park_boundary_symbol(
    stroke_hex: str,
    width_mm: float,
    dash_vector: list[float],
    *,
    band_hex: str | None = None,
    band_alpha: int = 44,
    band_width_mm: float = 0.95,
    band_offset_mm: float = 0.55,
    tint_alpha: int = 0,
) -> Any:
    """Create a crisp topo protected-area boundary (dashed perimeter + subtle inner ribbon)."""
    from qgis.core import (
        QgsFillSymbol,
        QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)

    if tint_alpha > 0 and band_hex is not None:
        fill_color = QColor(band_hex)
        fill_color.setAlpha(tint_alpha)
        bg = QgsSimpleFillSymbolLayer(fill_color)
        bg.setStrokeStyle(Qt.NoPen)
        sym.appendSymbolLayer(bg)

    if band_hex is not None and band_width_mm > 0:
        band_c = QColor(band_hex)
        band_c.setAlpha(band_alpha)
        ribbon = QgsSimpleLineSymbolLayer(band_c, band_width_mm)
        ribbon.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        ribbon.setOffset(band_offset_mm)
        ribbon.setOffsetUnit(QgsUnitTypes.RenderMillimeters)
        ribbon.setPenJoinStyle(Qt.RoundJoin)
        sym.appendSymbolLayer(ribbon)

    edge = QgsSimpleLineSymbolLayer(QColor(stroke_hex), width_mm)
    edge.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    edge.setPenCapStyle(Qt.FlatCap)
    edge.setPenJoinStyle(Qt.RoundJoin)
    edge.setUseCustomDashPattern(True)
    edge.setCustomDashVector(dash_vector)
    edge.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sym.appendSymbolLayer(edge)
    return sym


def _build_au_protected_areas_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style DCCEEW CAPAD Australian National Parks, Nature Reserves, and IPAs."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    ipa_expr = "upper(coalesce(\"type_abbr\", '')) = 'IPA' OR lower(coalesce(\"park_type\", '')) LIKE '%indigenous%'"

    root = QgsRuleBasedRenderer.Rule(None)
    if is_color:
        _add_rule(
            root,
            "Indigenous Protected Areas (IPA)",
            ipa_expr,
            _park_boundary_symbol(
                "#2E7D6B",
                0.48 * wf,
                [3.0, 1.1, 0.7, 1.1],
                band_hex="#2E7D6B",
                band_alpha=42,
                band_width_mm=0.95 * wf,
                band_offset_mm=0.52 * wf,
                tint_alpha=12,
            ),
        )
        _add_rule(
            root,
            "National Parks & Reserves (CAPAD)",
            f"NOT ({ipa_expr})",
            _park_boundary_symbol(
                "#2B7A3E",
                0.48 * wf,
                [3.4, 1.3],
                band_hex="#2B7A3E",
                band_alpha=46,
                band_width_mm=1.0 * wf,
                band_offset_mm=0.55 * wf,
                tint_alpha=14,
            ),
        )
    else:
        _add_rule(
            root,
            "Indigenous Protected Areas (IPA)",
            ipa_expr,
            _park_boundary_symbol(
                "#1A1A1A",
                0.46 * wf,
                [2.4, 1.0, 0.6, 1.0, 0.6, 1.0],
                band_hex="#666666",
                band_alpha=180,
                band_width_mm=0.14,
                band_offset_mm=0.55,
            ),
        )
        _add_rule(
            root,
            "National Parks & Reserves (CAPAD)",
            f"NOT ({ipa_expr})",
            _park_boundary_symbol(
                "#1A1A1A",
                0.48 * wf,
                [3.2, 1.1, 0.7, 1.1],
                band_hex="#555555",
                band_alpha=180,
                band_width_mm=0.14,
                band_offset_mm=0.55,
            ),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"type_abbr\" IS NOT NULL AND length(trim(\"type_abbr\")) > 0 "
        "THEN upper(trim(\"name\")) || ' (' || upper(trim(\"type_abbr\")) || ')' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN upper(trim(\"name\")) "
        "ELSE '' END"
    )
    pal.isExpression = True
    pal.priority = 4
    pal.setFormat(
        _make_text_format(
            6.6 + fd,
            "#1E5C2E" if is_color else "#1A1A1A",
            bold=True,
            halo_hex="#FAF9F5" if is_color else "#FFFFFF",
            halo_mm=0.75,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("CAPAD Protected Area Labels")
    rule.setFilterExpression("\"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_au_hydro_polys_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia Surface Hydrology polygons (lakes, waterholes, dry claypans)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    perennial_expr = "lower(coalesce(\"perenniality\", 'perennial')) = 'perennial'"

    root = QgsRuleBasedRenderer.Rule(None)
    if is_color:
        _add_rule(
            root,
            "GA Perennial Waterbody",
            perennial_expr,
            _simple_fill_symbol("#A6D1EC", stroke_hex="#3A82B5", stroke_mm=0.20 * wf, stroke_style="solid", fill_opacity=0.65),
        )
        _add_rule(
            root,
            "GA Non-Perennial / Dry Lake",
            f"NOT ({perennial_expr})",
            _hatch_fill_symbol(
                "#4B8EC2",
                angle_deg=0.0,
                spacing_mm=1.8,
                line_mm=0.12,
                bg_hex="#CBE3F2",
                bg_alpha=110,
                outline_hex="#3A82B5",
                outline_mm=0.18 * wf,
                outline_dash=[1.8, 1.0],
                dash_vector=[1.6, 1.2],
            ),
        )
    else:
        _add_rule(
            root,
            "GA Perennial Waterbody",
            perennial_expr,
            _hatch_fill_symbol(
                "#6B6B6B",
                angle_deg=0.0,
                spacing_mm=1.3,
                line_mm=0.12,
                bg_hex="#FFFFFF",
                outline_hex="#1A1A1A",
                outline_mm=0.22 * wf,
            ),
        )
        _add_rule(
            root,
            "GA Non-Perennial / Dry Lake",
            f"NOT ({perennial_expr})",
            _hatch_fill_symbol(
                "#777777",
                angle_deg=0.0,
                spacing_mm=1.9,
                line_mm=0.12,
                outline_hex="#333333",
                outline_mm=0.18 * wf,
                outline_dash=[1.8, 1.0],
                dash_vector=[1.4, 1.4],
            ),
        )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 5
    pal.setFormat(
        _make_text_format(
            6.2 + fd,
            "#1E5B88" if is_color else "#111111",
            italic=True,
            halo_hex="#FFFFFF",
            halo_mm=0.6,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("GA Waterbody Names")
    rule.setFilterExpression("\"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(carto.label_density != DensityLevel.LOW)


def _build_au_hydro_lines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia Surface Hydrology watercourses (perennial & non-perennial)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    is_color = color_mode == ColorMode.COLOR
    arrow_iv = 34.0 / wf

    major_c = "#2474B5" if is_color else "#1A1A1A"
    minor_c = "#2E80BF" if is_color else "#2B2B2B"
    nonper_c = "#3D8AC4" if is_color else "#444444"
    halo_c = "#FAF9F5" if is_color else "#FFFFFF"

    root = QgsRuleBasedRenderer.Rule(None)
    _add_rule(
        root,
        "GA Perennial Major Watercourse",
        "lower(coalesce(\"perenniality\", '')) = 'perennial' AND lower(coalesce(\"hierarchy\", '')) = 'major'",
        _waterway_symbol(
            major_c,
            0.46 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=1.9 * wf,
            arrow_color_hex=major_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "GA Perennial Minor Watercourse",
        "lower(coalesce(\"perenniality\", '')) = 'perennial' AND lower(coalesce(\"hierarchy\", '')) != 'major'",
        _waterway_symbol(
            minor_c,
            0.25 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv * 1.2,
            arrow_size_mm=1.55 * wf,
            arrow_color_hex=major_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "GA Non-Perennial / Ephemeral Creek",
        "lower(coalesce(\"perenniality\", '')) != 'perennial'",
        _line_symbol(nonper_c, 0.21 * wf, dash_vector=[2.5, 0.9, 0.5, 0.9]),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=72.0 * rep_mult, min_feature_mm=15.0 * min_mult)
    pal.setFormat(_make_text_format(5.8 + fd, major_c, italic=True, halo_hex=halo_c, halo_mm=0.55))
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("GA Watercourse Names")
    rule.setFilterExpression("\"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_au_hydro_points_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia Outback waterpoints (bores, springs, waterholes, tanks, dams)."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#1B6CA8" if is_color else "#000000"

    sym = _svg_marker_symbol("water_bore", 2.6 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"featuretype\" IS NOT NULL AND length(trim(\"featuretype\")) > 0 "
        "AND strpos(lower(\"name\"), lower(trim(\"featuretype\"))) = 0 "
        "THEN trim(\"name\") || ' (' || trim(\"featuretype\") || ')' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "WHEN \"featuretype\" IS NOT NULL AND length(trim(\"featuretype\")) > 0 THEN trim(\"featuretype\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 7
    _configure_point_pal(pal, over_point=False, dist_mm=1.6)
    pal.setFormat(
        _make_text_format(
            5.8 + fd,
            "#145282" if is_color else "#000000",
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.65,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_power_lines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia National Electricity Transmission Lines."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#4A4A4A" if is_color else "#1A1A1A"

    layer.setRenderer(QgsSingleSymbolRenderer(_powerline_symbol(c_hex, render_pass=2, wf=wf * 1.15)))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"capacity_kv\" IS NOT NULL AND to_real(\"capacity_kv\") > 0 "
        "THEN to_int(round(to_real(\"capacity_kv\"))) || ' kV' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=80.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    pal.setFormat(_make_text_format(5.5 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.55))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_substations_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia electricity substations."""
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#3D3D3D" if is_color else "#000000"

    sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#FFFFFF",
            "outline_color": c_hex,
            "outline_width": f"{0.38 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{2.2 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"voltage_kv\" IS NOT NULL AND to_real(\"voltage_kv\") > 0 "
        "THEN trim(\"name\") || ' (' || to_int(round(to_real(\"voltage_kv\"))) || ' kV)' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 6
    _configure_point_pal(pal, over_point=False, dist_mm=1.5)
    pal.setFormat(_make_text_format(5.6 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.6))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_pipelines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia Oil & Gas Pipelines."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#6A4C93" if is_color else "#1A1A1A"

    layer.setRenderer(QgsSingleSymbolRenderer(_pipeline_symbol(c_hex, render_pass=2, wf=wf * 1.05)))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "WHEN \"pipeline_type\" IS NOT NULL AND length(trim(\"pipeline_type\")) > 0 "
        "THEN upper(trim(\"pipeline_type\")) || ' PIPELINE' "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=85.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    pal.setFormat(_make_text_format(5.5 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.55))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_runways_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style Geoscience Australia regional aircraft runways."""
    from qgis.core import QgsSingleSymbolRenderer

    wf = scale_width_factor(scale)
    is_color = color_mode == ColorMode.COLOR
    sym = _cased_road_symbol(
        "#1A1A1A" if is_color else "#000000",
        1.55 * wf,
        "#666666" if is_color else "#444444",
        1.10 * wf,
        casing_pass=9,
        fill_pass=21,
        centerline_hex="#FFFFFF",
        centerline_mm=0.18 * wf,
        centerline_dash=[1.6, 1.1],
        centerline_pass=27,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_au_airfields_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia regional airfields & ALAs."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#1B4F72" if is_color else "#000000"

    sym = _svg_marker_symbol("airfield", 3.0 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 8
    _configure_point_pal(pal, over_point=False, dist_mm=1.8)
    pal.setFormat(_make_text_format(6.2 + fd, "#10324A" if is_color else "#000000", bold=True, halo_hex="#FFFFFF", halo_mm=0.7))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_homesteads_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia Outback homesteads & pastoral stations."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    c_hex = "#1A1A1A" if is_color else "#000000"

    sym = _svg_marker_symbol("homestead", 2.7 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 7
    _configure_point_pal(pal, over_point=False, dist_mm=1.7)
    pal.setFormat(_make_text_format(6.1 + fd, c_hex, bold=True, halo_hex="#FAF9F5" if is_color else "#FFFFFF", halo_mm=0.65))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _halo_dashed_line_symbol(
    stroke_hex: str,
    width_mm: float,
    dash_vector: list[float],
    *,
    halo_hex: str = "#FFFFFF",
    halo_width_mm: float = 0.0,
    halo_alpha: int = 215,
    render_pass: int = 0,
) -> Any:
    """Build a dashed line symbol optionally backed by a subtle contrast halo."""
    from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    if halo_width_mm > 0:
        hc = QColor(halo_hex)
        hc.setAlpha(halo_alpha)
        halo = QgsSimpleLineSymbolLayer(hc, halo_width_mm)
        halo.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        halo.setPenCapStyle(Qt.RoundCap)
        halo.setPenJoinStyle(Qt.RoundJoin)
        halo.setRenderingPass(max(0, render_pass - 1))
        sym.appendSymbolLayer(halo)

    dash = QgsSimpleLineSymbolLayer(QColor(stroke_hex), width_mm)
    dash.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    dash.setPenCapStyle(Qt.FlatCap)
    dash.setPenJoinStyle(Qt.RoundJoin)
    dash.setUseCustomDashPattern(True)
    dash.setCustomDashVector(dash_vector)
    dash.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    dash.setRenderingPass(render_pass)
    sym.appendSymbolLayer(dash)
    return sym


def _build_au_state_trails_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style NSW & VIC high-res Fire Trails, Management Trails, 4WD Tracks, and Walking Tracks."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    is_color = color_mode == ColorMode.COLOR
    halo_c = "#FAF9F5" if is_color else "#FFFFFF"

    fire_filter = "lower(coalesce(\"trail_class\", '')) IN ('fire_trail','management_trail')"
    fourwd_filter = "lower(coalesce(\"trail_class\", '')) = '4wd_track'"
    walk_filter = f"NOT ({fire_filter}) AND NOT ({fourwd_filter})"

    root = QgsRuleBasedRenderer.Rule(None)
    if is_color:
        _add_rule(
            root,
            "Fire & Management Trails",
            fire_filter,
            _halo_dashed_line_symbol(
                "#D95F02",
                0.56 * wf,
                [3.0, 1.1],
                halo_hex=halo_c,
                halo_width_mm=0.90 * wf,
                halo_alpha=200,
                render_pass=14,
            ),
        )
        _add_rule(
            root,
            "4WD Tracks",
            fourwd_filter,
            _line_symbol("#7F4F24", 0.42 * wf, dash_vector=[2.4, 1.0], cap_round=False, render_pass=10),
        )
        _add_rule(
            root,
            "Walking Tracks",
            walk_filter,
            _line_symbol("#A63603", 0.30 * wf, dash_vector=[1.4, 0.9], cap_round=False, render_pass=8),
        )
    else:
        _add_rule(
            root,
            "Fire & Management Trails",
            fire_filter,
            _halo_dashed_line_symbol(
                "#000000",
                0.55 * wf,
                [3.2, 0.9, 1.0, 0.9],
                halo_hex="#FFFFFF",
                halo_width_mm=0.92 * wf,
                halo_alpha=235,
                render_pass=14,
            ),
        )
        _add_rule(
            root,
            "4WD Tracks",
            fourwd_filter,
            _line_symbol("#1A1A1A", 0.40 * wf, dash_vector=[2.4, 1.0], cap_round=False, render_pass=10),
        )
        _add_rule(
            root,
            "Walking Tracks",
            walk_filter,
            _line_symbol("#1A1A1A", 0.28 * wf, dash_vector=[1.0, 0.9], cap_round=False, render_pass=8),
        )

    renderer = QgsRuleBasedRenderer(root)
    renderer.setUsingSymbolLevels(True)
    layer.setRenderer(renderer)

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)

    ft_pal = QgsPalLayerSettings()
    ft_pal.fieldName = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND lower(coalesce(\"trail_class\", '')) = 'fire_trail' "
        "AND strpos(lower(\"name\"), 'fire') = 0 "
        "AND strpos(lower(\"name\"), 'trail') = 0 "
        "AND strpos(lower(\"name\"), ' ft') = 0 "
        "THEN trim(\"name\") || ' (FT)' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "ELSE '' END"
    )
    ft_pal.isExpression = True
    ft_pal.priority = 7
    _configure_curved_line_pal(ft_pal, on_line=False, repeat_mm=65.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    ft_pal.setFormat(
        _make_text_format(
            5.9 + fd,
            "#A83F00" if is_color else "#000000",
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.68,
        )
    )
    ft_rule = QgsRuleBasedLabeling.Rule(ft_pal)
    ft_rule.setDescription("Fire Trail Labels")
    ft_rule.setFilterExpression(f"({fire_filter}) AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(ft_rule)

    trk_pal = QgsPalLayerSettings()
    trk_pal.fieldName = "name"
    trk_pal.isExpression = False
    trk_pal.priority = 5
    _configure_curved_line_pal(trk_pal, on_line=False, repeat_mm=75.0 * rep_mult, min_feature_mm=18.0 * min_mult)
    trk_pal.setFormat(
        _make_text_format(
            5.5 + fd,
            "#6B2A0A" if is_color else "#1A1A1A",
            italic=False,
            halo_hex=halo_c,
            halo_mm=0.58,
        )
    )
    trk_rule = QgsRuleBasedLabeling.Rule(trk_pal)
    trk_rule.setDescription("Walking & 4WD Track Labels")
    trk_rule.setFilterExpression(f"NOT ({fire_filter}) AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(trk_rule)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_au_state_pois_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style NSW & VIC Alpine Huts, Campgrounds, Trig Stations, Lookouts, and Picnic Sites."""
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    ink = "#1A1A1A" if is_color else "#000000"
    hut_c = "#7A3E1D" if is_color else "#000000"
    camp_c = "#1E6B3A" if is_color else "#000000"
    lookout_c = "#1B4F72" if is_color else "#111111"

    root = QgsRuleBasedRenderer.Rule(None)
    _add_rule(
        root,
        "Alpine Huts & Refuges",
        "lower(coalesce(\"poi_type\", '')) = 'hut'",
        _svg_marker_symbol("hut", 3.1 * wf, fill_hex=hut_c, outline_hex=ink, icons_root=icons_root),
    )
    _add_rule(
        root,
        "Campgrounds",
        "lower(coalesce(\"poi_type\", '')) = 'campground'",
        _svg_marker_symbol("campground", 3.0 * wf, fill_hex=camp_c, outline_hex=ink, icons_root=icons_root),
    )
    _add_rule(
        root,
        "Survey Trig Stations",
        "lower(coalesce(\"poi_type\", '')) = 'trig'",
        _svg_marker_symbol("trig", 2.9 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
    )
    _add_rule(
        root,
        "Lookouts",
        "lower(coalesce(\"poi_type\", '')) = 'lookout'",
        QgsMarkerSymbol.createSimple(
            {
                "name": "diamond",
                "color": lookout_c,
                "outline_color": "#FFFFFF",
                "outline_width": f"{0.32 * wf:.3f}",
                "outline_width_unit": "MM",
                "size": f"{2.3 * wf:.3f}",
                "size_unit": "MM",
            }
        ),
    )
    _add_rule(
        root,
        "Picnic & Rec Sites",
        "lower(coalesce(\"poi_type\", '')) NOT IN ('hut','campground','trig','lookout')",
        QgsMarkerSymbol.createSimple(
            {
                "name": "circle",
                "color": camp_c,
                "outline_color": "#FFFFFF",
                "outline_width": f"{0.30 * wf:.3f}",
                "outline_width_unit": "MM",
                "size": f"{2.0 * wf:.3f}",
                "size_unit": "MM",
            }
        ),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)

    if units == Units.IMPERIAL:
        elev_fmt = "to_int(round(to_real(\"elev_m\") * 3.28084)) || ' ft'"
    else:
        elev_fmt = "to_int(round(to_real(\"elev_m\"))) || ' m'"

    trig_pal = QgsPalLayerSettings()
    trig_pal.fieldName = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"elev_m\" IS NOT NULL AND to_real(\"elev_m\") > -500 "
        "AND regexp_match(coalesce(\"name\", ''), '(?i)[0-9]{2,4}\\\\s*(?:m|ft)\\\\b') = 0 "
        f"THEN trim(\"name\") || '\\n' || {elev_fmt} "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        f"WHEN \"elev_m\" IS NOT NULL AND to_real(\"elev_m\") > -500 THEN {elev_fmt} "
        "ELSE '' END"
    )
    trig_pal.isExpression = True
    trig_pal.priority = 8
    _configure_point_pal(trig_pal, over_point=False, dist_mm=1.8)
    trig_pal.setFormat(_make_text_format(5.9 + fd, ink, bold=True, halo_hex="#FFFFFF", halo_mm=0.7))
    trig_rule = QgsRuleBasedLabeling.Rule(trig_pal)
    trig_rule.setDescription("Trig Station Labels")
    trig_rule.setFilterExpression("lower(coalesce(\"poi_type\", '')) = 'trig'")
    label_root.appendChild(trig_rule)

    hc_pal = QgsPalLayerSettings()
    hc_pal.fieldName = "name"
    hc_pal.isExpression = False
    hc_pal.priority = 8
    _configure_point_pal(hc_pal, over_point=False, dist_mm=1.8)
    hc_pal.setFormat(
        _make_text_format(
            6.0 + fd,
            "#4A230D" if is_color else "#000000",
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.72,
        )
    )
    hc_rule = QgsRuleBasedLabeling.Rule(hc_pal)
    hc_rule.setDescription("Hut & Campground Labels")
    hc_rule.setFilterExpression(
        "lower(coalesce(\"poi_type\", '')) IN ('hut','campground') "
        "AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0"
    )
    label_root.appendChild(hc_rule)

    if carto.label_density != DensityLevel.LOW:
        rec_pal = QgsPalLayerSettings()
        rec_pal.fieldName = "name"
        rec_pal.isExpression = False
        rec_pal.priority = 6
        _configure_point_pal(rec_pal, over_point=False, dist_mm=1.6)
        rec_pal.setFormat(
            _make_text_format(
                5.5 + fd,
                lookout_c if is_color else "#1A1A1A",
                italic=True,
                halo_hex="#FFFFFF",
                halo_mm=0.6,
            )
        )
        rec_rule = QgsRuleBasedLabeling.Rule(rec_pal)
        rec_rule.setDescription("Lookout & Picnic Labels")
        rec_rule.setFilterExpression(
            "lower(coalesce(\"poi_type\", '')) NOT IN ('hut','campground','trig') "
            "AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0"
        )
        label_root.appendChild(rec_rule)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _waypoint_badge_symbol(
    shape: str,
    fill_hex: str,
    border_hex: str,
    size_mm: float,
) -> Any:
    """Build a high-contrast double-ring tactical waypoint marker."""
    from qgis.core import QgsMarkerSymbol, QgsSimpleMarkerSymbolLayer

    sym = QgsMarkerSymbol()
    sym.deleteSymbolLayer(0)

    outer = QgsSimpleMarkerSymbolLayer.create(
        {
            "name": shape,
            "color": "#FFFFFF",
            "outline_color": border_hex,
            "outline_width": "0.32",
            "outline_width_unit": "MM",
            "size": f"{size_mm + 0.95:.3f}",
            "size_unit": "MM",
        }
    )
    inner = QgsSimpleMarkerSymbolLayer.create(
        {
            "name": shape,
            "color": fill_hex,
            "outline_color": "#FFFFFF",
            "outline_width": "0.24",
            "outline_width_unit": "MM",
            "size": f"{size_mm:.3f}",
            "size_unit": "MM",
        }
    )
    sym.appendSymbolLayer(outer)
    sym.appendSymbolLayer(inner)
    return sym


def _build_waypoint_route_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style tactical waypoint connecting route legs (vivid magenta in Color, bold black in B&W)."""
    from qgis.core import QgsSingleSymbolRenderer

    wf = scale_width_factor(scale)
    is_color = color_mode == ColorMode.COLOR
    sym = _halo_dashed_line_symbol(
        "#C2185B" if is_color else "#111111",
        0.55 * wf,
        [3.2, 1.4],
        halo_hex="#FFFFFF",
        halo_width_mm=1.0 * wf,
        halo_alpha=220,
        render_pass=30,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_waypoint_points_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style numbered tactical waypoints (WP1..WPn) with category badges and top-priority callouts."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    sz = 2.6 * wf

    root = QgsRuleBasedRenderer.Rule(None)
    if is_color:
        _add_rule(
            root,
            "Camp Waypoint",
            "lower(coalesce(\"category\", '')) = 'camp'",
            _waypoint_badge_symbol("circle", "#1E7E34", "#0D4D1D", sz),
        )
        _add_rule(
            root,
            "Water Waypoint",
            "lower(coalesce(\"category\", '')) = 'water'",
            _waypoint_badge_symbol("circle", "#1565C0", "#0A3A70", sz),
        )
        _add_rule(
            root,
            "Hazard Waypoint",
            "lower(coalesce(\"category\", '')) = 'hazard'",
            _waypoint_badge_symbol("diamond", "#D84315", "#7F2307", sz + 0.3),
        )
        _add_rule(
            root,
            "LZ Waypoint",
            "lower(coalesce(\"category\", '')) = 'lz'",
            _waypoint_badge_symbol("pentagon", "#6A1B9A", "#3B0A59", sz + 0.2),
        )
        _add_rule(
            root,
            "Standard Waypoint",
            "lower(coalesce(\"category\", '')) NOT IN ('camp','water','hazard','lz')",
            _waypoint_badge_symbol("circle", "#C2185B", "#7A0D38", sz),
        )
    else:
        _add_rule(
            root,
            "Camp Waypoint",
            "lower(coalesce(\"category\", '')) = 'camp'",
            _waypoint_badge_symbol("square", "#111111", "#000000", sz),
        )
        _add_rule(
            root,
            "Water Waypoint",
            "lower(coalesce(\"category\", '')) = 'water'",
            _waypoint_badge_symbol("pentagon", "#111111", "#000000", sz + 0.15),
        )
        _add_rule(
            root,
            "Hazard Waypoint",
            "lower(coalesce(\"category\", '')) = 'hazard'",
            _waypoint_badge_symbol("diamond", "#000000", "#000000", sz + 0.35),
        )
        _add_rule(
            root,
            "LZ Waypoint",
            "lower(coalesce(\"category\", '')) = 'lz'",
            _waypoint_badge_symbol("triangle", "#000000", "#000000", sz + 0.25),
        )
        _add_rule(
            root,
            "Standard Waypoint",
            "lower(coalesce(\"category\", '')) NOT IN ('camp','water','hazard','lz')",
            _waypoint_badge_symbol("circle", "#111111", "#000000", sz),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))

    lbl_expr = (
        "CASE "
        "WHEN \"code\" IS NOT NULL AND length(trim(\"code\")) > 0 "
        "AND \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 "
        "AND lower(trim(\"label\")) != lower(trim(\"code\")) "
        "THEN trim(\"code\") || ' · ' || trim(\"label\") "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 THEN trim(\"label\") "
        "WHEN \"code\" IS NOT NULL AND length(trim(\"code\")) > 0 THEN trim(\"code\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 10
    _configure_point_pal(pal, over_point=False, dist_mm=2.2)
    pal.setFormat(
        _make_text_format(
            6.5 + fd,
            "#880E4F" if is_color else "#000000",
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.85,
            halo_opacity=0.98,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_viewshed_rings_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style RF / LOS viewshed max-range boundary rings."""
    from qgis.core import QgsSingleSymbolRenderer

    wf = scale_width_factor(scale)
    is_color = color_mode == ColorMode.COLOR
    sym = _halo_dashed_line_symbol(
        "#00897B" if is_color else "#222222",
        0.38 * wf,
        [2.6, 1.4],
        halo_hex="#FFFFFF",
        halo_width_mm=0.72 * wf,
        halo_alpha=185,
        render_pass=28,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_viewshed_observers_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    icons_root: Path | None = None,
) -> None:
    """Style RF / LOS observer beacon pins with AGL antenna height callouts."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    is_color = color_mode == ColorMode.COLOR
    fill_c = "#00695C" if is_color else "#000000"
    outline_c = "#004D40" if is_color else "#000000"

    sym = _svg_marker_symbol(
        "observer",
        3.3 * wf,
        fill_hex=fill_c,
        outline_hex=outline_c,
        icons_root=icons_root,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if units == Units.IMPERIAL:
        h_fmt = "to_int(round(to_real(\"observer_height_m\") * 3.28084)) || ' ft AGL'"
    else:
        h_fmt = (
            "CASE WHEN abs(to_real(\"observer_height_m\") - round(to_real(\"observer_height_m\"))) < 0.05 "
            "THEN to_int(round(to_real(\"observer_height_m\"))) "
            "ELSE round(to_real(\"observer_height_m\"), 1) END || ' m AGL'"
        )

    lbl_expr = (
        "CASE "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 AND \"observer_height_m\" IS NOT NULL "
        f"THEN trim(\"label\") || ' (' || {h_fmt} || ')' "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 THEN trim(\"label\") "
        f"WHEN \"observer_height_m\" IS NOT NULL THEN 'Observer (' || {h_fmt} || ')' "
        "ELSE 'Observer' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 10
    _configure_point_pal(pal, over_point=False, dist_mm=2.2)
    pal.setFormat(
        _make_text_format(
            6.3 + fd,
            "#004D40" if is_color else "#000000",
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.85,
            halo_opacity=0.98,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def apply_programmatic_style(
    layer: Any,
    role: str,
    color_mode: ColorMode,
    units: Units = Units.METRIC,
    *,
    scale: int = 25000,
    carto: CartoOptions | None = None,
    icons_root: Path | None = None,
) -> None:
    """Apply GridForge's programmatic PyQGIS 3.40 style for ``role``."""
    opts = carto if carto is not None else _DEFAULT_CARTO

    if role == "landcover":
        _build_landcover_style(layer, color_mode, carto=opts, icons_root=icons_root)
    elif role == "hillshade":
        _build_hillshade_style(layer, color_mode, carto=opts)
    elif role == "viewshed":
        _build_viewshed_style(layer, color_mode)
    elif role == "slope_shading":
        _build_slope_shading_style(layer, color_mode)
    elif role == "fema_flood":
        _build_fema_flood_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_protected_areas":
        _build_au_protected_areas_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "water_polygons":
        _build_water_polygons_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_hydro_polys":
        _build_au_hydro_polys_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "contours":
        _build_contours_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "enc_depth_contours":
        _build_enc_depth_contours_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "nhd_flowlines":
        _build_nhd_flowlines_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_hydro_lines":
        _build_au_hydro_lines_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "waterways":
        _build_waterways_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "buildings":
        _build_buildings_style(layer, color_mode, carto=opts)
    elif role == "building_icons":
        _build_building_icons_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "transport":
        _build_transport_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_state_trails":
        _build_au_state_trails_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_power_lines":
        _build_au_power_lines_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_pipelines":
        _build_au_pipelines_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_runways":
        _build_au_runways_style(layer, color_mode, scale=scale)
    elif role == "points":
        _build_points_style(layer, color_mode, units=units, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_state_pois":
        _build_au_state_pois_style(layer, color_mode, units=units, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_substations":
        _build_au_substations_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_hydro_points":
        _build_au_hydro_points_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_homesteads":
        _build_au_homesteads_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_airfields":
        _build_au_airfields_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "us_towers":
        _build_us_towers_style(layer, color_mode, units=units, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "enc_navaids":
        _build_enc_navaids_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
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
    elif role == "waypoint_route":
        _build_waypoint_route_style(layer, color_mode, scale=scale)
    elif role == "waypoint_points":
        _build_waypoint_points_style(layer, color_mode, scale=scale)
    elif role == "viewshed_rings":
        _build_viewshed_rings_style(layer, color_mode, scale=scale)
    elif role == "viewshed_observers":
        _build_viewshed_observers_style(layer, color_mode, units=units, scale=scale, icons_root=icons_root)
    else:
        raise ValueError(f"Unknown GridForge style role: {role!r}")


def _can_use_cached_qml(
    role: str,
    units: Units,
    scale: int,
    carto: CartoOptions,
) -> bool:
    """Return True when the static 1:25k default Metric ``.qml`` matches the requested options."""
    if role in ("mgrs_grid_1km", "mgrs_grid_100km", "mgrs_zone_boundary", "mgrs_square_labels"):
        return True
    if role in ("overlay_polygons", "overlay_lines", "overlay_points"):
        return True
    if scale not in (24000, 25000):
        return False
    if carto != _DEFAULT_CARTO:
        return False
    if units == Units.IMPERIAL and role in ("points", "us_towers", "au_state_pois", "viewshed_observers"):
        return False
    return True


def apply_layer_style(
    layer: Any,
    role: str,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions | None = None,
    styles_root: Path | None = None,
    icons_root: Path | None = None,
    prefer_qml: bool = True,
) -> bool:
    """Apply styling for ``role`` from versioned ``.qml`` with dynamic programmatic fallback.

    Returns ``True`` if loaded from a native QGIS 3.40 ``.qml`` file, ``False`` if applied
    programmatically. Dynamic ``CartoOptions`` overrides, non-25k multi-scale sizing, and
    Imperial unit expressions are applied programmatically so toggles never get masked by
    static QML files.
    """
    opts = carto if carto is not None else _DEFAULT_CARTO
    qml_path = get_qml_path(role, color_mode, styles_root=styles_root)
    use_qml = (
        prefer_qml
        and _can_use_cached_qml(role, units, scale, opts)
        and qml_path.is_file()
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
                if role == "hillshade":
                    _build_hillshade_style(layer, color_mode, carto=opts)
                elif role == "slope_shading":
                    _build_slope_shading_style(layer, color_mode)
                elif role == "viewshed":
                    _build_viewshed_style(layer, color_mode)
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

    apply_programmatic_style(
        layer,
        role,
        color_mode,
        units=units,
        scale=scale,
        carto=opts,
        icons_root=icons_root,
    )
    return False


def _list_gpkg_layers(gpkg_path: Path) -> list[str]:
    """List vector layer names present in a GeoPackage using OGR."""
    from osgeo import ogr

    ogr.UseExceptions()
    if not gpkg_path.is_file():
        return []
    ds = ogr.Open(str(gpkg_path))
    if ds is None:
        return []
    try:
        return [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    finally:
        ds = None


def _load_vector_layer(
    project: Any,
    gpkg_path: Path,
    layer_name: str,
    display_name: str,
    *,
    role: str,
    workspace: JobWorkspace,
) -> Any | None:
    from qgis.core import QgsVectorLayer

    if not gpkg_path.is_file():
        return None
    uri = f"{gpkg_path}|layername={layer_name}"
    vlayer = QgsVectorLayer(uri, display_name, "ogr")
    if not vlayer.isValid():
        logger.debug("Skipping unavailable vector layer %s (%s)", display_name, uri)
        return None

    spec = workspace.layout.spec
    apply_layer_style(
        vlayer,
        role,
        spec.color_mode,
        units=spec.units,
        scale=spec.scale,
        carto=spec.carto,
    )
    project.addMapLayer(vlayer, False)
    return vlayer


def _load_overlay_layers(project: Any, workspace: JobWorkspace) -> list[Any]:
    """Load optional user overlay GeoPackage layers ordered Points -> Lines -> Polygons."""
    from qgis.core import Qgis, QgsVectorLayer, QgsWkbTypes

    if workspace.overlays_gpkg is None or not workspace.overlays_gpkg.is_file():
        return []

    layer_names = _list_gpkg_layers(workspace.overlays_gpkg)
    points_layers: list[Any] = []
    lines_layers: list[Any] = []
    poly_layers: list[Any] = []
    spec = workspace.layout.spec

    for lname in layer_names:
        uri = f"{workspace.overlays_gpkg}|layername={lname}"
        vlayer = QgsVectorLayer(uri, f"overlay_{lname}", "ogr")
        if not vlayer.isValid():
            continue

        geom_type = vlayer.geometryType()
        point_enum = getattr(Qgis.GeometryType, "Point", QgsWkbTypes.PointGeometry)
        line_enum = getattr(Qgis.GeometryType, "Line", QgsWkbTypes.LineGeometry)
        poly_enum = getattr(Qgis.GeometryType, "Polygon", QgsWkbTypes.PolygonGeometry)

        if geom_type == point_enum:
            role = "overlay_points"
            target_bucket = points_layers
        elif geom_type == line_enum:
            role = "overlay_lines"
            target_bucket = lines_layers
        elif geom_type == poly_enum:
            role = "overlay_polygons"
            target_bucket = poly_layers
        else:
            continue

        apply_layer_style(
            vlayer,
            role,
            spec.color_mode,
            units=spec.units,
            scale=spec.scale,
            carto=spec.carto,
        )
        project.addMapLayer(vlayer, False)
        target_bucket.append(vlayer)

    return points_layers + lines_layers + poly_layers


def populate_project_layers(
    project: Any,
    workspace: JobWorkspace,
    *,
    include_grid: bool | None = None,
) -> list[Any]:
    """Load and style all workspace layers in exact top-to-bottom QGIS map order.

    Shared by BOTH print PDF rendering (``pyqgis_worker.py``) and Phase 3 collarless
    GeoTIFF / MBTiles / Garmin KMZ raster export (``export/raster.py``).

    Index 0 is drawn LAST (on top); last index is drawn FIRST (bottom).
    Hillshade (Multiply) sits immediately above landcover polygons so every layer
    above hillshade remains 100% crisp un-rasterized vector graphics in PDF export.
    """
    from qgis.core import QgsCoordinateReferenceSystem, QgsRasterLayer

    spec = workspace.layout.spec
    carto = spec.carto
    show_grid = carto.show_grid if include_grid is None else include_grid

    project.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{workspace.layout.utm_epsg}"))

    osm_layers = set(_list_gpkg_layers(workspace.osm_gpkg))
    mgrs_layers = set(_list_gpkg_layers(workspace.mgrs_gpkg))
    terrain_layers = set(_list_gpkg_layers(workspace.terrain_gpkg))

    regional = workspace.regional_overlays or {}
    fema_path = Path(regional["fema"]) if "fema" in regional else None
    nhd_path = Path(regional["nhd"]) if "nhd" in regional else None
    towers_path = Path(regional["towers"]) if "towers" in regional else None
    enc_path = Path(regional["enc"]) if "enc" in regional else None

    au_hydro_path = Path(regional["au-hydro"]) if "au-hydro" in regional else (
        Path(regional["au_hydro"]) if "au_hydro" in regional else None
    )
    au_parks_path = Path(regional["au-parks"]) if "au-parks" in regional else (
        Path(regional["au_parks"]) if "au_parks" in regional else None
    )
    au_infra_path = Path(regional["au-infra"]) if "au-infra" in regional else (
        Path(regional["au_infra"]) if "au_infra" in regional else None
    )
    au_aviation_path = Path(regional["au-aviation"]) if "au-aviation" in regional else (
        Path(regional["au_aviation"]) if "au_aviation" in regional else None
    )

    au_state_trail_entries: list[tuple[str, Path, set[str]]] = []
    seen_state_paths: set[Path] = set()
    unified_trail_kinds = (*AU_STATE_TRAIL_OVERLAY_KINDS, NZOverlayKind.NZ_DOC_TRAILS)
    for st_kind in unified_trail_kinds:
        raw_st = regional.get(st_kind.value) or regional.get(st_kind.value.replace("-", "_"))
        if raw_st is None:
            continue
        st_path = Path(raw_st)
        resolved_st = st_path.resolve()
        if resolved_st in seen_state_paths:
            continue
        seen_state_paths.add(resolved_st)
        st_layers = set(_list_gpkg_layers(st_path))
        if st_layers:
            if st_kind.value == "au-state-trails":
                label_prefix = "AU State"
            elif st_kind.value == "nz-doc-trails":
                label_prefix = "NZ DOC"
            else:
                label_prefix = st_kind.value.split("-")[1].upper()
            au_state_trail_entries.append((label_prefix, st_path, st_layers))

    fema_layers = set(_list_gpkg_layers(fema_path)) if fema_path else set()
    nhd_layers = set(_list_gpkg_layers(nhd_path)) if nhd_path else set()
    towers_layers = set(_list_gpkg_layers(towers_path)) if towers_path else set()
    enc_layers = set(_list_gpkg_layers(enc_path)) if enc_path else set()
    au_hydro_layers = set(_list_gpkg_layers(au_hydro_path)) if au_hydro_path else set()
    au_parks_layers = set(_list_gpkg_layers(au_parks_path)) if au_parks_path else set()
    au_infra_layers = set(_list_gpkg_layers(au_infra_path)) if au_infra_path else set()
    au_aviation_layers = set(_list_gpkg_layers(au_aviation_path)) if au_aviation_path else set()
    waypoints_path = workspace.waypoints_gpkg if (workspace.waypoints_gpkg and workspace.waypoints_gpkg.is_file()) else None
    waypoints_layers = set(_list_gpkg_layers(waypoints_path)) if waypoints_path else set()
    vs_obs_path = (
        workspace.viewshed_observers_gpkg
        if (workspace.viewshed_observers_gpkg and workspace.viewshed_observers_gpkg.is_file())
        else None
    )
    vs_obs_layers = set(_list_gpkg_layers(vs_obs_path)) if vs_obs_path else set()

    ordered_top_to_bottom: list[Any] = []

    def _push(layer: Any | None) -> None:
        if layer is not None:
            ordered_top_to_bottom.append(layer)

    # 0. Tactical Custom Waypoints & RF/LOS Viewshed Observers on very top (highest visibility)
    if waypoints_path:
        if "waypoint_points" in waypoints_layers:
            _push(
                _load_vector_layer(
                    project, waypoints_path, "waypoint_points", "Waypoints", role="waypoint_points", workspace=workspace
                )
            )
        if "waypoint_route" in waypoints_layers:
            _push(
                _load_vector_layer(
                    project, waypoints_path, "waypoint_route", "Waypoint Route", role="waypoint_route", workspace=workspace
                )
            )

    if vs_obs_path:
        if "viewshed_observers" in vs_obs_layers:
            _push(
                _load_vector_layer(
                    project,
                    vs_obs_path,
                    "viewshed_observers",
                    "LOS / RF Observers",
                    role="viewshed_observers",
                    workspace=workspace,
                )
            )
        if "viewshed_rings" in vs_obs_layers:
            _push(
                _load_vector_layer(
                    project,
                    vs_obs_path,
                    "viewshed_rings",
                    "LOS / RF Max Range",
                    role="viewshed_rings",
                    workspace=workspace,
                )
            )

    # 1. User overlays (uploaded GPX/GeoJSON routes & POIs)
    ordered_top_to_bottom.extend(_load_overlay_layers(project, workspace))

    # 2. MGRS Grid stack (100% pure vector, BlendNormal; toggleable via carto.show_grid / include_grid)
    if show_grid:
        if "square_labels" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "square_labels", "MGRS Square IDs", role="mgrs_square_labels", workspace=workspace
                )
            )
        if "zone_boundary" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "zone_boundary", "UTM Zone Boundary", role="mgrs_zone_boundary", workspace=workspace
                )
            )
        if "grid_100km" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "grid_100km", "MGRS 100 km Grid", role="mgrs_grid_100km", workspace=workspace
                )
            )
        if "grid_1km" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "grid_1km", "MGRS 1 km Grid", role="mgrs_grid_1km", workspace=workspace
                )
            )

    # 3. Regional Point Overlays (US Obstacles/Navaids + AU National + Pan-AU State Huts/Camps/Trigs)
    if towers_path and "us_towers" in towers_layers:
        _push(_load_vector_layer(project, towers_path, "us_towers", "US Obstacle Towers", role="us_towers", workspace=workspace))
    if enc_path and "enc_navaids" in enc_layers:
        _push(_load_vector_layer(project, enc_path, "enc_navaids", "NOAA ENC Navaids", role="enc_navaids", workspace=workspace))
    for label_prefix, st_path, st_layers in au_state_trail_entries:
        if "au_state_pois" in st_layers:
            _push(
                _load_vector_layer(
                    project,
                    st_path,
                    "au_state_pois",
                    f"{label_prefix} Huts, Camps & Trigs",
                    role="au_state_pois",
                    workspace=workspace,
                )
            )
    if au_aviation_path and "au_airfields" in au_aviation_layers:
        _push(_load_vector_layer(project, au_aviation_path, "au_airfields", "AU Airfields", role="au_airfields", workspace=workspace))
    if au_aviation_path and "au_homesteads" in au_aviation_layers:
        _push(_load_vector_layer(project, au_aviation_path, "au_homesteads", "AU Homesteads", role="au_homesteads", workspace=workspace))
    if au_hydro_path and "au_hydro_points" in au_hydro_layers:
        _push(_load_vector_layer(project, au_hydro_path, "au_hydro_points", "AU Waterpoints & Bores", role="au_hydro_points", workspace=workspace))
    if au_infra_path and "au_substations" in au_infra_layers and carto.show_power:
        _push(_load_vector_layer(project, au_infra_path, "au_substations", "AU Substations", role="au_substations", workspace=workspace))

    # 4. OSM Points & Building-Polygon Usage Icons
    if "points" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "points", "Points & Places", role="points", workspace=workspace))
    if "multipolygons" in osm_layers:
        _push(
            _load_vector_layer(
                project, workspace.osm_gpkg, "multipolygons", "Building Usage Icons", role="building_icons", workspace=workspace
            )
        )

    # 5. OSM Transport & Infrastructure + AU Runways / Pan-AU State Fire Trails & Tracks / HV Power / Pipelines
    if au_aviation_path and "au_runways" in au_aviation_layers:
        _push(_load_vector_layer(project, au_aviation_path, "au_runways", "AU Runways", role="au_runways", workspace=workspace))
    for label_prefix, st_path, st_layers in au_state_trail_entries:
        if "au_state_trails" in st_layers:
            _push(
                _load_vector_layer(
                    project,
                    st_path,
                    "au_state_trails",
                    f"{label_prefix} Fire Trails & Tracks",
                    role="au_state_trails",
                    workspace=workspace,
                )
            )
    if "lines" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "lines", "Roads & Transport", role="transport", workspace=workspace))
    if au_infra_path and "au_power_lines" in au_infra_layers and carto.show_power:
        _push(_load_vector_layer(project, au_infra_path, "au_power_lines", "AU Transmission Lines", role="au_power_lines", workspace=workspace))
    if au_infra_path and "au_pipelines" in au_infra_layers and carto.show_pipelines:
        _push(_load_vector_layer(project, au_infra_path, "au_pipelines", "AU Pipelines", role="au_pipelines", workspace=workspace))

    # 6. OSM Buildings & Power Substations
    if "multipolygons" in osm_layers and (carto.show_buildings or carto.show_power):
        _push(_load_vector_layer(project, workspace.osm_gpkg, "multipolygons", "Buildings", role="buildings", workspace=workspace))

    # 7. Waterways (OSM + optional USGS NHD + optional GA Hydrology + optional NOAA ENC Depth Contours)
    if "lines" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "lines", "Waterways", role="waterways", workspace=workspace))
    if au_hydro_path and "au_hydro_lines" in au_hydro_layers:
        _push(_load_vector_layer(project, au_hydro_path, "au_hydro_lines", "AU Watercourses", role="au_hydro_lines", workspace=workspace))
    if nhd_path and "nhd_flowlines" in nhd_layers:
        _push(_load_vector_layer(project, nhd_path, "nhd_flowlines", "USGS NHD Flowlines", role="nhd_flowlines", workspace=workspace))
    if enc_path and "enc_depth_contours" in enc_layers:
        _push(
            _load_vector_layer(
                project, enc_path, "enc_depth_contours", "NOAA ENC Depth Contours", role="enc_depth_contours", workspace=workspace
            )
        )

    # 8. Elevation Contours (Index + Minor with curved haloed labels)
    if "contours" in terrain_layers:
        _push(_load_vector_layer(project, workspace.terrain_gpkg, "contours", "Contours", role="contours", workspace=workspace))

    # 9. Water Polygons (OSM + optional GA Surface Hydrology polygons; above hillshade)
    if "multipolygons" in osm_layers:
        _push(
            _load_vector_layer(
                project, workspace.osm_gpkg, "multipolygons", "Waterbodies", role="water_polygons", workspace=workspace
            )
        )
    if au_hydro_path and "au_hydro_polys" in au_hydro_layers:
        _push(_load_vector_layer(project, au_hydro_path, "au_hydro_polys", "AU Waterbodies", role="au_hydro_polys", workspace=workspace))

    # 10. Regional Area Overlays (FEMA Flood Zones + CAPAD AU Parks/IPAs; strictly below contours & roads!)
    if fema_path and "fema_flood" in fema_layers:
        _push(_load_vector_layer(project, fema_path, "fema_flood", "FEMA Flood Zones", role="fema_flood", workspace=workspace))
    if au_parks_path and "au_protected_areas" in au_parks_layers:
        _push(_load_vector_layer(project, au_parks_path, "au_protected_areas", "AU Protected Areas", role="au_protected_areas", workspace=workspace))

    # 10.5. Slope-Angle Avalanche Shading (bottom raster stack right above viewshed & hillshade; vectors stay pure!)
    if carto.show_slope_shading and workspace.slope_tif is not None and workspace.slope_tif.is_file():
        slope_layer = QgsRasterLayer(str(workspace.slope_tif), "Slope Shading")
        if slope_layer.isValid():
            apply_layer_style(
                slope_layer,
                "slope_shading",
                spec.color_mode,
                units=spec.units,
                scale=spec.scale,
                carto=carto,
            )
            project.addMapLayer(slope_layer, False)
            ordered_top_to_bottom.append(slope_layer)

    # 10.75. RF / Line-of-Sight Viewshed Coverage (bottom raster stack right above hillshade, below slope!)
    if workspace.viewshed_tif is not None and workspace.viewshed_tif.is_file():
        vs_layer = QgsRasterLayer(str(workspace.viewshed_tif), "LOS Viewshed")
        if vs_layer.isValid():
            apply_layer_style(
                vs_layer,
                "viewshed",
                spec.color_mode,
                units=spec.units,
                scale=spec.scale,
                carto=carto,
            )
            project.addMapLayer(vs_layer, False)
            ordered_top_to_bottom.append(vs_layer)

    # 11. Multi-directional Hillshade (Multiply blend over landcover + background; omitted if OFF)
    if carto.hillshade_strength != HillshadeStrength.OFF and workspace.hillshade_tif.is_file():
        hs_layer = QgsRasterLayer(str(workspace.hillshade_tif), "Hillshade")
        if hs_layer.isValid():
            apply_layer_style(
                hs_layer,
                "hillshade",
                spec.color_mode,
                units=spec.units,
                scale=spec.scale,
                carto=carto,
            )
            project.addMapLayer(hs_layer, False)
            ordered_top_to_bottom.append(hs_layer)

    # 12. OSM Landcover Polygons (bottom layer — receives Multiply hillshade & slope shading!)
    if "multipolygons" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "multipolygons", "Landcover", role="landcover", workspace=workspace))

    return ordered_top_to_bottom


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
            "&field=leaf_type:string&field=wood:string&field=leaf_cycle:string"
            "&field=water:string&field=waterway:string&field=building:string"
            "&field=amenity:string&field=religion:string&field=emergency:string"
            "&field=leisure:string&field=aeroway:string&field=power:string"
            "&field=man_made:string&field=boundary:string",
            "landcover",
        ),
        "fema_flood": (
            "MultiPolygon?crs=EPSG:32613&field=fld_zone:string&field=zone_subty:string&field=sfha_tf:string",
            "fema_flood",
        ),
        "au_protected_areas": (
            "MultiPolygon?crs=EPSG:32752"
            "&field=name:string&field=park_type:string&field=type_abbr:string&field=iucn:string&field=state:string",
            "au_protected_areas",
        ),
        "water_polygons": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=name:string&field=natural:string&field=landuse:string"
            "&field=water:string&field=waterway:string",
            "water_polygons",
        ),
        "au_hydro_polys": (
            "MultiPolygon?crs=EPSG:32752&field=name:string&field=featuretype:string&field=perenniality:string",
            "au_hydro_polys",
        ),
        "contours": (
            "LineString?crs=EPSG:32756&field=elev:double&field=elev_label:string&field=is_index:integer",
            "contours",
        ),
        "enc_depth_contours": (
            "LineString?crs=EPSG:32613&field=valdco:double",
            "enc_depth_contours",
        ),
        "nhd_flowlines": (
            "LineString?crs=EPSG:32613&field=gnis_name:string&field=fcode:integer&field=flow_type:string",
            "nhd_flowlines",
        ),
        "au_hydro_lines": (
            "LineString?crs=EPSG:32752"
            "&field=name:string&field=featuretype:string&field=perenniality:string&field=hierarchy:string",
            "au_hydro_lines",
        ),
        "waterways": (
            "LineString?crs=EPSG:32756&field=name:string&field=waterway:string&field=intermittent:string",
            "waterways",
        ),
        "buildings": (
            "MultiPolygon?crs=EPSG:32756&field=name:string&field=building:string&field=power:string",
            "buildings",
        ),
        "building_icons": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=name:string&field=building:string&field=amenity:string"
            "&field=emergency:string&field=aeroway:string&field=religion:string",
            "building_icons",
        ),
        "transport": (
            "LineString?crs=EPSG:32756"
            "&field=name:string&field=highway:string&field=ref:string&field=surface:string"
            "&field=tracktype:string&field=railway:string&field=aeroway:string"
            "&field=power:string&field=voltage:string&field=man_made:string"
            "&field=substance:string&field=location:string&field=bridge:string"
            "&field=tunnel:string&field=layer:string&field=oneway:string",
            "transport",
        ),
        "au_power_lines": (
            "LineString?crs=EPSG:32756&field=name:string&field=capacity_kv:double&field=status:string&field=state:string",
            "au_power_lines",
        ),
        "au_state_trails": (
            "LineString?crs=EPSG:32756&field=name:string&field=trail_class:string&field=surface:string&field=state:string",
            "au_state_trails",
        ),
        "au_pipelines": (
            "LineString?crs=EPSG:32756&field=name:string&field=pipeline_type:string&field=status:string&field=operator:string",
            "au_pipelines",
        ),
        "au_runways": (
            "LineString?crs=EPSG:32752&field=featuretype:string&field=type:string",
            "au_runways",
        ),
        "points": (
            "Point?crs=EPSG:32756"
            "&field=name:string&field=highway:string&field=ref:string&field=exit_to:string"
            "&field=junction:string&field=railway:string&field=aeroway:string"
            "&field=natural:string&field=ele:string&field=amenity:string&field=building:string"
            "&field=religion:string&field=emergency:string&field=shop:string"
            "&field=historic:string&field=place:string&field=power:string"
            "&field=man_made:string&field=tower:type:string&field=communication:string"
            "&field=tourism:string",
            "points",
        ),
        "au_state_pois": (
            "Point?crs=EPSG:32756&field=name:string&field=poi_type:string&field=elev_m:double&field=state:string",
            "au_state_pois",
        ),
        "au_substations": (
            "Point?crs=EPSG:32756&field=name:string&field=voltage_kv:double&field=status:string&field=state:string",
            "au_substations",
        ),
        "au_hydro_points": (
            "Point?crs=EPSG:32752&field=name:string&field=featuretype:string&field=type:string&field=perenniality:string",
            "au_hydro_points",
        ),
        "au_homesteads": (
            "Point?crs=EPSG:32752&field=name:string&field=featuretype:string",
            "au_homesteads",
        ),
        "au_airfields": (
            "Point?crs=EPSG:32752&field=name:string&field=featuretype:string&field=type:string",
            "au_airfields",
        ),
        "us_towers": (
            "Point?crs=EPSG:32613"
            "&field=source:string&field=name:string&field=obstacle_type:string"
            "&field=agl_ft:double&field=amsl_ft:double&field=lighting:string",
            "us_towers",
        ),
        "enc_navaids": (
            "Point?crs=EPSG:32613&field=objnam:string&field=navaid_type:string&field=colour:string",
            "enc_navaids",
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
        "waypoint_route": ("LineString?crs=EPSG:32756&field=total_km:double", "waypoint_route"),
        "waypoint_points": (
            "Point?crs=EPSG:32756&field=seq:integer&field=code:string&field=label:string"
            "&field=category:string&field=mgrs_10m:string&field=elev_m:double",
            "waypoint_points",
        ),
        "viewshed_rings": (
            "LineString?crs=EPSG:32756&field=label:string&field=max_distance_m:double",
            "viewshed_rings",
        ),
        "viewshed_observers": (
            "Point?crs=EPSG:32756&field=label:string&field=observer_height_m:double"
            "&field=target_height_m:double&field=max_distance_m:double&field=elev_m:double",
            "viewshed_observers",
        ),
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

        dummy_slope_tif = Path(tmpdir) / "dummy_slope_rgba.tif"
        sds = drv.Create(str(dummy_slope_tif), 4, 4, 4, gdal.GDT_Byte)
        sds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        for b_idx, val in ((1, 229), (2, 62), (3, 62), (4, 185)):
            sds.GetRasterBand(b_idx).Fill(val)
        sds.FlushCache()
        sds = None

        dummy_vs_tif = Path(tmpdir) / "dummy_viewshed_rgba.tif"
        vds = drv.Create(str(dummy_vs_tif), 4, 4, 4, gdal.GDT_Byte)
        vds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        for b_idx, val in ((1, 0), (2, 212), (3, 146), (4, 130)):
            vds.GetRasterBand(b_idx).Fill(val)
        vds.FlushCache()
        vds = None

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

            rlayer = QgsRasterLayer(str(dummy_tif), "hillshade")
            apply_programmatic_style(rlayer, "hillshade", mode)
            hs_qml = mode_dir / "hillshade.qml"
            msg, ok = rlayer.saveNamedStyle(str(hs_qml))
            if not ok:
                raise RuntimeError(f"Failed to export {hs_qml}: {msg}")
            _tag_native_qml(hs_qml)
            written.append(hs_qml)

            vlayer_vs = QgsRasterLayer(str(dummy_vs_tif), "viewshed")
            apply_programmatic_style(vlayer_vs, "viewshed", mode)
            vs_qml = mode_dir / "viewshed.qml"
            msg, ok = vlayer_vs.saveNamedStyle(str(vs_qml))
            if not ok:
                raise RuntimeError(f"Failed to export {vs_qml}: {msg}")
            _tag_native_qml(vs_qml)
            written.append(vs_qml)

            slayer = QgsRasterLayer(str(dummy_slope_tif), "slope_shading")
            apply_programmatic_style(slayer, "slope_shading", mode)
            slope_qml = mode_dir / "slope_shading.qml"
            msg, ok = slayer.saveNamedStyle(str(slope_qml))
            if not ok:
                raise RuntimeError(f"Failed to export {slope_qml}: {msg}")
            _tag_native_qml(slope_qml)
            written.append(slope_qml)

            for role, (uri, layer_name) in vector_specs.items():
                vlayer = QgsVectorLayer(uri, layer_name, "memory")
                apply_programmatic_style(vlayer, role, mode, units=Units.METRIC, scale=25000)
                qml_path = mode_dir / f"{role}.qml"
                msg, ok = vlayer.saveNamedStyle(str(qml_path))
                if not ok:
                    raise RuntimeError(f"Failed to export {qml_path}: {msg}")
                _tag_native_qml(qml_path)
                written.append(qml_path)

    return written


if __name__ == "__main__":
    import gc
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
        gc.collect()
        qgs.exitQgis()
