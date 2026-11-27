"""Fast pure-Pillow dark-tactical PNG visual card generators for GridForge MCP tools.

Strictly zero ``qgis`` / ``PyQt5`` imports; renders crisp PNG schematics in < 50 ms
and <= ~120 KB so MCP ``CallToolResult`` payloads remain safely under trajectory caps.
"""

from __future__ import annotations

import functools
import io
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# Dark-tactical color palette
_BG = (11, 17, 23)
_SURFACE = (17, 26, 36)
_SURFACE_ALT = (21, 32, 44)
_HEADER_BG = (14, 22, 31)
_BORDER = (38, 53, 71)
_GRID = (30, 44, 58)
_MGRS_LINE = (42, 92, 118)
_MGRS_100K = (56, 189, 248)
_INK = (232, 238, 245)
_MUTED = (143, 163, 184)
_DIM = (96, 118, 140)

_EMERALD = (34, 197, 94)
_EMERALD_SOFT = (74, 222, 128)
_AMBER = (245, 158, 11)
_CRIMSON = (239, 68, 68)
_CYAN = (56, 189, 248)
_SKY = (96, 165, 250)
_ORANGE = (251, 146, 60)
_PURPLE = (192, 132, 252)

_TERRAIN_FILL = (40, 66, 56)
_TERRAIN_LINE = (110, 231, 183)

_WP_COLORS: dict[str, tuple[int, int, int]] = {
    "waypoint": _AMBER,
    "camp": _EMERALD,
    "water": _CYAN,
    "hazard": _CRIMSON,
    "lz": _PURPLE,
}

_FONT_CANDIDATES: dict[tuple[bool, bool], tuple[str, ...]] = {
    (False, False): (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ),
    (True, False): (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ),
    (False, True): (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
    ),
    (True, True): (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    ),
}


@functools.lru_cache(maxsize=64)
def _load_font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES[(bold, mono)]:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    rgb.save(buf, format="PNG", optimize=True, compress_level=6)
    return buf.getvalue()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: Any) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return (int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1]))


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    *,
    bg: tuple[int, int, int],
    fg: tuple[int, int, int],
    border: tuple[int, int, int] | None = None,
    font: Any,
    pad_x: int = 8,
    pad_y: int = 3,
    align_right: bool = False,
) -> tuple[int, int, int, int]:
    tw, th = _text_size(draw, text, font)
    pw = tw + pad_x * 2
    ph = th + pad_y * 2
    x0 = x - pw if align_right else x
    y0 = y
    draw.rounded_rectangle(
        [x0, y0, x0 + pw, y0 + ph],
        radius=5,
        fill=bg,
        outline=border or bg,
        width=1,
    )
    draw.text((x0 + pad_x, y0 + pad_y - 1), text, fill=fg, font=font)
    return (x0, y0, x0 + pw, y0 + ph)


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    pts: list[tuple[float, float]],
    *,
    fill: tuple[int, int, int],
    width: int = 1,
    dash: float = 6.0,
    gap: float = 4.0,
) -> None:
    if len(pts) < 2:
        return
    step = dash + gap
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        dx = x1 - x0
        dy = y1 - y0
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-3:
            continue
        ux = dx / seg_len
        uy = dy / seg_len
        pos = 0.0
        while pos < seg_len:
            end = min(seg_len, pos + dash)
            draw.line(
                [(x0 + ux * pos, y0 + uy * pos), (x0 + ux * end, y0 + uy * end)],
                fill=fill,
                width=width,
            )
            pos += step


def _format_ew(angle_deg: float) -> str:
    return f"{abs(angle_deg):.1f}° {'E' if angle_deg >= 0 else 'W'}"


def _truncate(text: str, max_chars: int) -> str:
    s = str(text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(1, max_chars - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# 1. RF Link Fresnel Profile Chart (760 x 360)
# ---------------------------------------------------------------------------


def render_rf_link_chart_png(profile_dict: dict[str, Any]) -> bytes:
    """Render a crisp 760x360 dark-tactical PNG chart of a curved-Earth RF Fresnel link profile."""
    W, H = 760, 360
    img = Image.new("RGBA", (W, H), (*_BG, 255))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    odraw = ImageDraw.Draw(overlay)

    f_title = _load_font(14, bold=True)
    f_sub = _load_font(11, bold=False, mono=True)
    f_badge = _load_font(11, bold=True, mono=True)
    f_axis = _load_font(10, mono=True)
    f_small = _load_font(10, bold=False)
    f_small_b = _load_font(10, bold=True)

    status = str(profile_dict.get("status") or "CLEAR").upper()
    from_label = str(profile_dict.get("from_label") or "TX")
    to_label = str(profile_dict.get("to_label") or "RX")
    freq_mhz = float(profile_dict.get("freq_mhz") or 915.0)
    dist_km = float(profile_dict.get("distance_km") or 0.0)
    fspl_db = float(profile_dict.get("fspl_db") or 0.0)
    min_clr = float(profile_dict.get("min_clearance_m") or 0.0)
    min_f1_pct = float(profile_dict.get("min_fresnel_pct") or 0.0)
    b_true = float(profile_dict.get("bearing_true_deg") or 0.0)
    b_grid = float(profile_dict.get("bearing_grid_deg") or 0.0)
    b_mag = float(profile_dict.get("bearing_mag_deg") or 0.0)
    b_mils = float(profile_dict.get("bearing_mils") or 0.0)
    from_elev = float(profile_dict.get("from_elev_m") or 0.0)
    to_elev = float(profile_dict.get("to_elev_m") or 0.0)
    from_h = float(profile_dict.get("from_height_m") or 2.0)
    to_h = float(profile_dict.get("to_height_m") or 2.0)
    from_mgrs = str(profile_dict.get("from_mgrs") or "")
    to_mgrs = str(profile_dict.get("to_mgrs") or "")
    samples: list[dict[str, Any]] = list(profile_dict.get("samples") or [])

    if status == "OBSTRUCTED":
        status_bg, status_fg = (127, 29, 29), (254, 202, 202)
        status_border = _CRIMSON
        los_col = _CRIMSON
    elif status == "MARGINAL":
        status_bg, status_fg = (120, 53, 15), (253, 230, 138)
        status_border = _AMBER
        los_col = _AMBER
    else:
        status_bg, status_fg = (20, 83, 45), (187, 247, 208)
        status_border = _EMERALD
        los_col = _EMERALD_SOFT

    # Outer border & header bar
    draw.rectangle([0, 0, W - 1, H - 1], outline=_BORDER, width=1)
    draw.rectangle([1, 1, W - 2, 42], fill=_HEADER_BG)
    draw.line([(1, 42), (W - 2, 42)], fill=_BORDER, width=1)

    header_title = _truncate(f"RF LINK PROFILE · {from_label} → {to_label}", 56)
    draw.text((14, 12), header_title, fill=_INK, font=f_title)
    _draw_pill(
        draw,
        W - 14,
        10,
        f" {status} ",
        bg=status_bg,
        fg=status_fg,
        border=status_border,
        font=f_badge,
        align_right=True,
    )

    # Telemetry KPI strip
    draw.rectangle([1, 43, W - 2, 72], fill=_SURFACE)
    draw.line([(1, 72), (W - 2, 72)], fill=_BORDER, width=1)
    clr_sign = "+" if min_clr >= 0 else ""
    kpi_left = (
        f"{freq_mhz:g} MHz   ·   {dist_km:.2f} km   ·   "
        f"FSPL {fspl_db:.1f} dB   ·   Min Clr {clr_sign}{min_clr:.1f} m ({min_f1_pct:.0f}% F1)"
    )
    kpi_right = f"T {b_true:05.1f}°  G {b_grid:05.1f}°  M {b_mag:05.1f}° ({b_mils:.0f} mil)"
    draw.text((14, 51), kpi_left, fill=_INK, font=f_sub)
    rw, _ = _text_size(draw, kpi_right, f_axis)
    draw.text((W - 14 - rw, 52), kpi_right, fill=_CYAN, font=f_axis)

    # Plot rectangle
    px0, py0, px1, py1 = 62, 86, 738, 298
    pw = px1 - px0
    ph = py1 - py0
    draw.rectangle([px0, py0, px1, py1], fill=_SURFACE, outline=_BORDER, width=1)

    if samples:
        max_d = max(
            dist_km,
            max((float(s.get("dist_km", 0.0)) for s in samples), default=1.0),
            0.05,
        )
        all_y: list[float] = []
        for s in samples:
            for key in ("terrain_m", "los_m", "fresnel_lo_m", "fresnel_hi_m"):
                v = s.get(key)
                if v is not None and math.isfinite(float(v)):
                    all_y.append(float(v))
        y_lo = min(all_y) if all_y else 0.0
        y_hi = max(all_y) if all_y else 100.0
        span = max(y_hi - y_lo, 20.0)
        y_min = y_lo - span * 0.14
        y_max = y_hi + span * 0.16
    else:
        max_d = max(dist_km, 1.0)
        y_min, y_max = 0.0, 100.0

    def _sx(d: float) -> float:
        return px0 + (max(0.0, min(max_d, d)) / max_d) * pw

    def _sy(elev: float) -> float:
        return py1 - ((elev - y_min) / (y_max - y_min)) * ph

    # Horizontal elevation gridlines
    n_y = 4
    for i in range(n_y + 1):
        val = y_min + (y_max - y_min) * i / n_y
        yy = _sy(val)
        draw.line([(px0 + 1, yy), (px1 - 1, yy)], fill=_GRID, width=1)
        lbl = f"{round(val):,}m"
        tw, th = _text_size(draw, lbl, f_axis)
        draw.text((px0 - 6 - tw, yy - th / 2), lbl, fill=_MUTED, font=f_axis)

    # Vertical distance gridlines
    n_x = 5
    for i in range(n_x + 1):
        dv = max_d * i / n_x
        xx = _sx(dv)
        if 0 < i < n_x:
            _draw_dashed_line(draw, [(xx, py0 + 1), (xx, py1 - 1)], fill=_GRID, width=1, dash=3, gap=3)
        lbl = f"{dv:.1f} km"
        tw, _ = _text_size(draw, lbl, f_axis)
        lx = max(px0, min(px1 - tw, xx - tw / 2))
        draw.text((lx, py1 + 6), lbl, fill=_MUTED, font=f_axis)

    if len(samples) >= 2:
        # 100% 1st Fresnel zone envelope
        f1_hi_pts = [(_sx(float(s["dist_km"])), _sy(float(s["fresnel_hi_m"]))) for s in samples]
        f1_lo_pts = [(_sx(float(s["dist_km"])), _sy(float(s["fresnel_lo_m"]))) for s in samples]
        f1_poly = f1_hi_pts + list(reversed(f1_lo_pts))
        odraw.polygon(f1_poly, fill=(*_SKY, 36))

        # 60% inner Fresnel clearance envelope (LOS down to fresnel60_lo)
        los_pts = [(_sx(float(s["dist_km"])), _sy(float(s["los_m"]))) for s in samples]
        f60_lo_pts = [(_sx(float(s["dist_km"])), _sy(float(s["fresnel60_lo_m"]))) for s in samples]
        f60_fill = (*_EMERALD, 38) if status == "CLEAR" else (*_AMBER, 42)
        odraw.polygon(los_pts + list(reversed(f60_lo_pts)), fill=f60_fill)

        # Terrain polygon
        terr_pts = [(_sx(float(s["dist_km"])), _sy(float(s["terrain_m"]))) for s in samples]
        terr_poly = [(terr_pts[0][0], float(py1)), *terr_pts, (terr_pts[-1][0], float(py1))]
        odraw.polygon(terr_poly, fill=(*_TERRAIN_FILL, 225))

        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)

        # Crisp strokes on top
        _draw_dashed_line(draw, f1_hi_pts, fill=(96, 165, 250), width=1, dash=4, gap=3)
        _draw_dashed_line(draw, f1_lo_pts, fill=(96, 165, 250), width=1, dash=4, gap=3)
        draw.line(f60_lo_pts, fill=_AMBER if status != "CLEAR" else (52, 211, 153), width=1)
        draw.line(terr_pts, fill=_TERRAIN_LINE, width=2)
        if status == "OBSTRUCTED":
            _draw_dashed_line(draw, los_pts, fill=los_col, width=2, dash=7, gap=4)
        else:
            draw.line(los_pts, fill=los_col, width=2)

        # TX and RX towers
        s_start, s_end = samples[0], samples[-1]
        for pt_s, lbl, elev_v, ant_h, align_r in (
            (s_start, from_label, from_elev, from_h, False),
            (s_end, to_label, to_elev, to_h, True),
        ):
            tx = _sx(float(pt_s["dist_km"]))
            ty_ground = _sy(float(pt_s["terrain_m"]))
            ty_ant = _sy(float(pt_s["los_m"]))
            draw.line([(tx, ty_ground), (tx, ty_ant)], fill=_INK, width=2)
            draw.ellipse([tx - 4, ty_ant - 4, tx + 4, ty_ant + 4], fill=_CYAN, outline=_BG, width=1)
            tag = f"{_truncate(lbl, 12)} ({round(elev_v)}m +{ant_h:g}m)"
            tw, th = _text_size(draw, tag, f_small_b)
            tx_lbl = (tx - tw - 6) if align_r else (tx + 6)
            tx_lbl = max(px0 + 4, min(px1 - tw - 4, tx_lbl))
            ty_lbl = max(py0 + 4, min(py1 - th - 4, ty_ant - th - 4))
            draw.rectangle([tx_lbl - 3, ty_lbl - 1, tx_lbl + tw + 3, ty_lbl + th + 2], fill=_BG)
            draw.text((tx_lbl, ty_lbl), tag, fill=_INK, font=f_small_b)

        # Minimum clearance / obstacle marker
        inner = samples[1:-1] if len(samples) > 2 else samples
        worst = min(inner, key=lambda s: float(s.get("clearance_m", 0.0)))
        wx = _sx(float(worst["dist_km"]))
        wy_terr = _sy(float(worst["terrain_m"]))
        wy_los = _sy(float(worst["los_m"]))
        w_clr = float(worst.get("clearance_m", 0.0))
        marker_col = _CRIMSON if w_clr < 0 else (_AMBER if status == "MARGINAL" else _EMERALD_SOFT)
        draw.line([(wx, wy_terr), (wx, wy_los)], fill=marker_col, width=2)
        draw.ellipse([wx - 4, wy_terr - 4, wx + 4, wy_terr + 4], fill=marker_col, outline=_BG, width=1)
        w_tag = (
            f"OBSTACLE {w_clr:.1f}m @ {float(worst['dist_km']):.2f}km"
            if w_clr < 0
            else f"MIN CLR +{w_clr:.1f}m @ {float(worst['dist_km']):.2f}km"
        )
        wt, wh = _text_size(draw, w_tag, f_small_b)
        bx = max(px0 + 6, min(px1 - wt - 10, wx - wt / 2))
        by = max(py0 + 6, min(wy_los, wy_terr) - wh - 12)
        draw.rounded_rectangle([bx - 4, by - 2, bx + wt + 4, by + wh + 3], radius=4, fill=_HEADER_BG, outline=marker_col, width=1)
        draw.text((bx, by), w_tag, fill=marker_col, font=f_small_b)
    else:
        msg = "No elevation samples available"
        mw, mh = _text_size(draw, msg, f_sub)
        draw.text(((px0 + px1 - mw) / 2, (py0 + py1 - mh) / 2), msg, fill=_MUTED, font=f_sub)

    # Footer legend & MGRS endpoints
    draw.line([(1, 326), (W - 2, 326)], fill=_BORDER, width=1)
    draw.rectangle([1, 327, W - 2, H - 2], fill=_HEADER_BG)

    lx = 14
    for col, label in (
        (_TERRAIN_LINE, "Terrain"),
        (los_col, "LOS Ray"),
        (_SKY, "100% F1"),
        (_AMBER, "60% F1"),
    ):
        draw.rectangle([lx, 340, lx + 10, 346], fill=col)
        draw.text((lx + 14, 336), label, fill=_MUTED, font=f_small)
        tw, _ = _text_size(draw, label, f_small)
        lx += tw + 28

    if from_mgrs or to_mgrs:
        mgrs_txt = f"TX {from_mgrs or '-'}  |  RX {to_mgrs or '-'}"
        mw, _ = _text_size(draw, mgrs_txt, f_axis)
        draw.text((W - 14 - mw, 337), mgrs_txt, fill=_DIM, font=f_axis)

    return _encode_png(img)


# ---------------------------------------------------------------------------
# 2. True-Scale Footprint / Viewshed / Declination Card (720 x 460)
# ---------------------------------------------------------------------------


def _collect_geojson_coords(geojson: dict[str, Any] | None) -> list[tuple[float, float]]:
    """Return all ``(lon, lat)`` coordinate pairs inside a GeoJSON object."""
    if not isinstance(geojson, dict):
        return []
    out: list[tuple[float, float]] = []

    def _walk_geom(geom: dict[str, Any] | None) -> None:
        if not isinstance(geom, dict):
            return
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
            out.append((float(coords[0]), float(coords[1])))
        elif gtype == "LineString" and isinstance(coords, list):
            for c in coords:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    out.append((float(c[0]), float(c[1])))
        elif gtype in ("MultiLineString", "Polygon") and isinstance(coords, list):
            for ring in coords:
                if isinstance(ring, list):
                    for c in ring:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            out.append((float(c[0]), float(c[1])))
        elif gtype == "MultiPolygon" and isinstance(coords, list):
            for poly in coords:
                if isinstance(poly, list):
                    for ring in poly:
                        if isinstance(ring, list):
                            for c in ring:
                                if isinstance(c, (list, tuple)) and len(c) >= 2:
                                    out.append((float(c[0]), float(c[1])))
        elif gtype == "GeometryCollection":
            for sub in geom.get("geometries") or []:
                _walk_geom(sub)

    dtype = geojson.get("type")
    if dtype == "FeatureCollection":
        for feat in geojson.get("features") or []:
            if isinstance(feat, dict):
                _walk_geom(feat.get("geometry"))
    elif dtype == "Feature":
        _walk_geom(geojson.get("geometry"))
    else:
        _walk_geom(geojson)
    return out


def _iter_geojson_features(geojson: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(geojson, dict):
        return []
    dtype = geojson.get("type")
    if dtype == "FeatureCollection":
        return [f for f in (geojson.get("features") or []) if isinstance(f, dict)]
    if dtype == "Feature":
        return [geojson]
    if dtype in ("Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"):
        return [{"type": "Feature", "properties": {}, "geometry": geojson}]
    return []


def _make_geo_projector(
    coords: list[tuple[float, float]],
    box: tuple[int, int, int, int],
    pad_frac: float = 0.12,
):
    x0, y0, x1, y1 = box
    w = max(1, x1 - x0)
    h = max(1, y1 - y0)
    if not coords:
        return lambda lon, lat: (x0 + w / 2.0, y0 + h / 2.0)

    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    mean_lat = 0.5 * (min_lat + max_lat)
    cos_lat = max(0.15, math.cos(math.radians(mean_lat)))

    span_x = max((max_lon - min_lon) * cos_lat, 1e-4)
    span_y = max(max_lat - min_lat, 1e-4)

    eff_w = w * (1.0 - 2.0 * pad_frac)
    eff_h = h * (1.0 - 2.0 * pad_frac)
    scale = min(eff_w / span_x, eff_h / span_y)

    cx_geo = 0.5 * (min_lon + max_lon)
    cy_geo = 0.5 * (min_lat + max_lat)
    cx_px = 0.5 * (x0 + x1)
    cy_px = 0.5 * (y0 + y1)

    def _proj(lon: float, lat: float) -> tuple[float, float]:
        px = cx_px + (lon - cx_geo) * cos_lat * scale
        py = cy_px - (lat - cy_geo) * scale
        return (px, py)

    return _proj


def _draw_declination_compass(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    decl: dict[str, Any] | None,
) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle([x0, y0, x1, y1], radius=6, fill=_SURFACE, outline=_BORDER, width=1)
    f_hdr = _load_font(10, bold=True)
    f_mono = _load_font(10, mono=True)
    f_lbl = _load_font(10, bold=True, mono=True)

    model_name = str((decl or {}).get("model_name") or "WMM2025")
    mag_deg = float((decl or {}).get("magnetic_declination_deg") or 0.0)
    grid_conv = float((decl or {}).get("grid_convergence_deg") or 0.0)
    gm_deg = float((decl or {}).get("gm_angle_deg") or 0.0)
    gm_mils = float((decl or {}).get("gm_angle_mils") or 0.0)

    draw.text((x0 + 10, y0 + 7), f"NORTH · {model_name}", fill=_MUTED, font=f_hdr)

    # Compass origin inside upper half of box
    cx = (x0 + x1) / 2.0
    cy = y0 + 102.0
    arm = 62.0

    # Clamp visual deflection angles so small angles stay clearly distinguishable
    def _visual_rad(deg: float) -> float:
        if abs(deg) < 0.05:
            return 0.0
        sign = 1.0 if deg >= 0 else -1.0
        vis_deg = sign * min(34.0, max(7.0, abs(deg) * 1.45))
        return math.radians(vis_deg)

    # True North (straight up, star marker)
    tn_top = (cx, cy - arm)
    draw.line([(cx, cy), tn_top], fill=_INK, width=2)
    draw.ellipse([tn_top[0] - 3, tn_top[1] - 3, tn_top[0] + 3, tn_top[1] + 3], fill=_INK)
    draw.text((tn_top[0] - 7, tn_top[1] - 16), "TN", fill=_INK, font=f_lbl)

    # Grid North (cyan)
    gn_rad = _visual_rad(grid_conv)
    gn_top = (cx + math.sin(gn_rad) * (arm - 4), cy - math.cos(gn_rad) * (arm - 4))
    draw.line([(cx, cy), gn_top], fill=_CYAN, width=2)
    draw.text((gn_top[0] - (18 if grid_conv < 0 else -4), gn_top[1] - 12), "GN", fill=_CYAN, font=f_lbl)

    # Magnetic North (red/amber arrow)
    mn_rad = _visual_rad(mag_deg)
    mn_top = (cx + math.sin(mn_rad) * (arm - 2), cy - math.cos(mn_rad) * (arm - 2))
    draw.line([(cx, cy), mn_top], fill=_CRIMSON, width=2)
    # Arrowhead
    ah = 7.0
    for side in (-0.42, 0.42):
        ax = mn_top[0] - math.sin(mn_rad + side) * ah
        ay = mn_top[1] + math.cos(mn_rad + side) * ah
        draw.line([mn_top, (ax, ay)], fill=_CRIMSON, width=2)
    draw.text((mn_top[0] + (5 if mag_deg >= 0 else -22), mn_top[1] - 6), "MN", fill=_CRIMSON, font=f_lbl)

    # Pivot dot
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=_AMBER)

    # Numeric readouts
    ry = y0 + 114
    rows = [
        ("Mag Decl", _format_ew(mag_deg), _CRIMSON),
        ("Grid Conv", _format_ew(grid_conv), _CYAN),
        ("G-M Angle", f"{_format_ew(gm_deg)} ({abs(gm_mils):.0f}m)", _AMBER),
    ]
    for k, v, col in rows:
        draw.text((x0 + 10, ry), k, fill=_MUTED, font=f_mono)
        vw, _ = _text_size(draw, v, f_mono)
        draw.text((x1 - 10 - vw, ry), v, fill=col, font=f_mono)
        ry += 16


def render_footprint_card_png(
    footprint_dict: dict[str, Any],
    *,
    title: str | None = None,
    waypoints: list[dict[str, Any]] | None = None,
    viewshed: dict[str, Any] | None = None,
) -> bytes:
    """Render a crisp 720x460 dark-tactical PNG map schematic with MGRS grid, sheets & declination."""
    W, H = 720, 460
    img = Image.new("RGBA", (W, H), (*_BG, 255))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    odraw = ImageDraw.Draw(overlay)

    f_title = _load_font(14, bold=True)
    f_badge = _load_font(10, bold=True, mono=True)
    f_mono = _load_font(10, mono=True)
    f_small = _load_font(9, mono=True)
    f_sheet = _load_font(13, bold=True, mono=True)
    f_hdr = _load_font(10, bold=True)

    center_mgrs = str(
        footprint_dict.get("center_mgrs_10m")
        or (viewshed or {}).get("observer_mgrs_10m")
        or ""
    )
    center_lat = footprint_dict.get("center_lat")
    center_lon = footprint_dict.get("center_lon")
    utm_epsg = footprint_dict.get("utm_epsg") or (viewshed or {}).get("utm_epsg")
    utm_zone = footprint_dict.get("utm_zone")
    utm_hemi = footprint_dict.get("utm_hemisphere") or ""
    gzd = str(footprint_dict.get("gzd") or "")
    sq_ids = list(footprint_dict.get("square_ids_100km") or [])
    grid_cols = int(footprint_dict.get("grid_cols") or 1)
    grid_rows = int(footprint_dict.get("grid_rows") or 1)
    sheet_count = int(footprint_dict.get("sheet_count") or (grid_cols * grid_rows))
    gw_km = float(footprint_dict.get("total_ground_width_km") or footprint_dict.get("ground_width_km") or 0.0)
    gh_km = float(footprint_dict.get("total_ground_height_km") or footprint_dict.get("ground_height_km") or 0.0)
    grid_spacing_mm = float(footprint_dict.get("grid_spacing_mm") or 0.0)
    decl = footprint_dict.get("declination") if isinstance(footprint_dict.get("declination"), dict) else None

    fp_geo = footprint_dict.get("footprint_geojson")
    grid_geo = footprint_dict.get("grid_geojson")

    # Infer scale/paper from first footprint feature if available
    scale_val = footprint_dict.get("scale")
    paper_val = footprint_dict.get("paper")
    orient_val = footprint_dict.get("orientation")
    for feat in _iter_geojson_features(fp_geo):
        props = feat.get("properties") or {}
        scale_val = scale_val or props.get("scale")
        paper_val = paper_val or props.get("paper")
        orient_val = orient_val or props.get("orientation")
        break

    # Outer border + header bar
    draw.rectangle([0, 0, W - 1, H - 1], outline=_BORDER, width=1)
    draw.rectangle([1, 1, W - 2, 44], fill=_HEADER_BG)
    draw.line([(1, 44), (W - 2, 44)], fill=_BORDER, width=1)

    header_title = _truncate(
        title or (f"VIEWSHED · {center_mgrs}" if viewshed else f"GRIDFORGE SHEET · {center_mgrs or 'FOOTPRINT'}"),
        42,
    )
    draw.text((14, 13), header_title, fill=_INK, font=f_title)

    # Header pills (right-aligned)
    rx = W - 12
    if utm_epsg:
        utm_lbl = f"EPSG:{utm_epsg}" + (f" · {gzd}" if gzd else "")
        b = _draw_pill(draw, rx, 11, utm_lbl, bg=_SURFACE_ALT, fg=_CYAN, border=_BORDER, font=f_badge, align_right=True)
        rx = b[0] - 6
    if scale_val:
        sc_lbl = f"1:{int(scale_val):,}"
        b = _draw_pill(draw, rx, 11, sc_lbl, bg=(45, 34, 14), fg=_AMBER, border=_AMBER, font=f_badge, align_right=True)
        rx = b[0] - 6
    if paper_val:
        p_lbl = f"{str(paper_val).upper()}" + (f" {str(orient_val)[:4].upper()}" if orient_val else "")
        _draw_pill(draw, rx, 11, p_lbl, bg=_SURFACE_ALT, fg=_INK, border=_BORDER, font=f_badge, align_right=True)

    # Map panel
    map_box = (14, 56, 516, 408)
    mx0, my0, mx1, my1 = map_box
    draw.rounded_rectangle([mx0, my0, mx1, my1], radius=6, fill=_SURFACE, outline=_BORDER, width=1)

    all_coords: list[tuple[float, float]] = []
    all_coords.extend(_collect_geojson_coords(fp_geo))
    all_coords.extend(_collect_geojson_coords(grid_geo))
    if isinstance(viewshed, dict):
        all_coords.extend(_collect_geojson_coords(viewshed.get("range_ring_geojson")))
        all_coords.extend(_collect_geojson_coords(viewshed.get("viewshed_geojson")))
        obs = viewshed.get("observer") if isinstance(viewshed.get("observer"), dict) else None
        if obs and obs.get("lat") is not None and obs.get("lon") is not None:
            all_coords.append((float(obs["lon"]), float(obs["lat"])))
    for wp in waypoints or []:
        if wp.get("lat") is not None and wp.get("lon") is not None:
            all_coords.append((float(wp["lon"]), float(wp["lat"])))
    if not all_coords and center_lat is not None and center_lon is not None:
        clat, clon = float(center_lat), float(center_lon)
        all_coords = [(clon - 0.02, clat - 0.02), (clon + 0.02, clat + 0.02)]

    proj = _make_geo_projector(all_coords, (mx0 + 18, my0 + 18, mx1 - 18, my1 - 24), pad_frac=0.06)

    # 1. Viewshed coverage polygons (translucent emerald)
    if isinstance(viewshed, dict):
        for feat in _iter_geojson_features(viewshed.get("viewshed_geojson")):
            geom = feat.get("geometry") or {}
            gtype = geom.get("type")
            polys = []
            if gtype == "Polygon":
                polys = [geom.get("coordinates") or []]
            elif gtype == "MultiPolygon":
                polys = list(geom.get("coordinates") or [])
            for poly in polys:
                if poly and poly[0]:
                    pts = [proj(float(c[0]), float(c[1])) for c in poly[0] if len(c) >= 2]
                    if len(pts) >= 3:
                        odraw.polygon(pts, fill=(*_EMERALD, 82), outline=(*_EMERALD_SOFT, 190))

    # 2. Footprint sheet fills (subtle translucent tint)
    sheet_polys: list[tuple[str, bool, list[tuple[float, float]]]] = []
    for feat in _iter_geojson_features(fp_geo):
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        if geom.get("type") == "Polygon":
            coords_rings = geom.get("coordinates") or []
            if coords_rings and coords_rings[0]:
                pts = [proj(float(c[0]), float(c[1])) for c in coords_rings[0] if len(c) >= 2]
                if len(pts) >= 3:
                    sid = str(props.get("sheet_id") or props.get("label") or "A1")
                    is_primary = bool(props.get("is_primary"))
                    fill_rgba = (*_AMBER, 28) if is_primary else (*_CYAN, 18)
                    odraw.polygon(pts, fill=fill_rgba)
                    sheet_polys.append((sid, is_primary, pts))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # 3. 1 km MGRS grid lines + de-cluttered principal digit ticks
    last_e_x = -999.0
    last_n_y = -999.0
    for feat in _iter_geojson_features(grid_geo):
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        if geom.get("type") != "LineString":
            continue
        raw_pts = geom.get("coordinates") or []
        pts = [proj(float(c[0]), float(c[1])) for c in raw_pts if len(c) >= 2]
        if len(pts) < 2:
            continue
        is_100k = bool(props.get("is_100km"))
        draw.line(pts, fill=_MGRS_100K if is_100k else _MGRS_LINE, width=2 if is_100k else 1)

        digits = str(props.get("principal_digits") or "")
        axis = str(props.get("axis") or "")
        if digits and axis == "E":
            # Place Easting principal digits near top of line
            tx, ty = pts[-1] if pts[-1][1] < pts[0][1] else pts[0]
            if abs(tx - last_e_x) >= 22 and mx0 + 8 <= tx <= mx1 - 18:
                draw.text((tx - 6, max(my0 + 4, ty - 12)), digits, fill=_MGRS_100K if is_100k else _DIM, font=f_small)
                last_e_x = tx
        elif digits and axis == "N":
            # Place Northing principal digits near left end of line
            tx, ty = pts[0] if pts[0][0] < pts[-1][0] else pts[-1]
            if abs(ty - last_n_y) >= 16 and my0 + 10 <= ty <= my1 - 18:
                draw.text((max(mx0 + 4, tx - 16), ty - 5), digits, fill=_MGRS_100K if is_100k else _DIM, font=f_small)
                last_n_y = ty

    # 4. Viewshed range ring (dashed) & observer beacon
    if isinstance(viewshed, dict):
        for feat in _iter_geojson_features(viewshed.get("range_ring_geojson")):
            geom = feat.get("geometry") or {}
            gtype = geom.get("type")
            ring_pts: list[tuple[float, float]] = []
            if gtype == "LineString":
                ring_pts = [proj(float(c[0]), float(c[1])) for c in (geom.get("coordinates") or []) if len(c) >= 2]
            elif gtype == "Polygon":
                rings = geom.get("coordinates") or []
                if rings and rings[0]:
                    ring_pts = [proj(float(c[0]), float(c[1])) for c in rings[0] if len(c) >= 2]
            if len(ring_pts) >= 2:
                _draw_dashed_line(draw, ring_pts, fill=_CYAN, width=2, dash=6, gap=4)

        obs = viewshed.get("observer") if isinstance(viewshed.get("observer"), dict) else None
        if obs and obs.get("lat") is not None and obs.get("lon") is not None:
            ox, oy = proj(float(obs["lon"]), float(obs["lat"]))
            draw.ellipse([ox - 7, oy - 7, ox + 7, oy + 7], outline=_AMBER, width=2)
            draw.ellipse([ox - 3, oy - 3, ox + 3, oy + 3], fill=_AMBER)
            obs_lbl = _truncate(str(obs.get("label") or "OBS"), 14)
            draw.text((ox + 9, oy - 6), obs_lbl, fill=_AMBER, font=f_badge)

    # 5. Footprint sheet outlines & centered ID badges
    for sid, is_primary, pts in sheet_polys:
        stroke_col = _AMBER if is_primary else _CYAN
        draw.line(pts, fill=stroke_col, width=2 if is_primary else 1)
        # Centroid of exterior ring (exclude closing dup)
        unique = pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else pts
        cx = sum(p[0] for p in unique) / max(1, len(unique))
        cy = sum(p[1] for p in unique) / max(1, len(unique))
        if len(sheet_polys) > 1 or not viewshed:
            tw, th = _text_size(draw, sid, f_sheet)
            draw.rounded_rectangle(
                [cx - tw / 2 - 5, cy - th / 2 - 2, cx + tw / 2 + 5, cy + th / 2 + 3],
                radius=4,
                fill=_HEADER_BG,
                outline=stroke_col,
                width=1,
            )
            draw.text((cx - tw / 2, cy - th / 2 - 1), sid, fill=stroke_col, font=f_sheet)

    # 6. Optional waypoints + connecting legs
    if waypoints:
        wp_px: list[tuple[float, float, dict[str, Any]]] = []
        for wp in waypoints:
            if wp.get("lat") is not None and wp.get("lon") is not None:
                wx, wy = proj(float(wp["lon"]), float(wp["lat"]))
                wp_px.append((wx, wy, wp))
        if len(wp_px) >= 2:
            _draw_dashed_line(
                draw,
                [(p[0], p[1]) for p in wp_px],
                fill=_ORANGE,
                width=2,
                dash=6,
                gap=4,
            )
        for idx, (wx, wy, wp) in enumerate(wp_px, start=1):
            cat = str(wp.get("category") or "waypoint").lower()
            col = _WP_COLORS.get(cat, _AMBER)
            draw.ellipse([wx - 6, wy - 6, wx + 6, wy + 6], fill=col, outline=_BG, width=1)
            num = str(idx)
            nw, nh = _text_size(draw, num, f_small)
            draw.text((wx - nw / 2, wy - nh / 2 - 1), num, fill=_BG, font=f_small)
            w_lbl = _truncate(str(wp.get("label") or f"WP{idx}"), 14)
            draw.text((wx + 8, wy - 6), w_lbl, fill=_INK, font=f_small)

    # 7. Center crosshair reticle (when no viewshed observer already marks it)
    if center_lat is not None and center_lon is not None and not viewshed:
        cx, cy = proj(float(center_lon), float(center_lat))
        if mx0 + 4 <= cx <= mx1 - 4 and my0 + 4 <= cy <= my1 - 4:
            draw.line([(cx - 8, cy), (cx - 3, cy)], fill=_AMBER, width=1)
            draw.line([(cx + 3, cy), (cx + 8, cy)], fill=_AMBER, width=1)
            draw.line([(cx, cy - 8), (cx, cy - 3)], fill=_AMBER, width=1)
            draw.line([(cx, cy + 3), (cx, cy + 8)], fill=_AMBER, width=1)

    # Map panel bottom-left extent badge
    if gw_km > 0 and gh_km > 0:
        ext_txt = (
            f"{sheet_count} sheets ({grid_cols}x{grid_rows}) · {gw_km:.2f} × {gh_km:.2f} km"
            if sheet_count > 1
            else f"Footprint {gw_km:.2f} × {gh_km:.2f} km"
        )
        draw.text((mx0 + 10, my1 - 16), ext_txt, fill=_MUTED, font=f_small)

    # Right telemetry sidebar
    _draw_declination_compass(draw, (528, 56, 706, 224), decl)

    # Telemetry metadata card
    tx0, ty0, tx1, ty1 = 528, 234, 706, 408
    draw.rounded_rectangle([tx0, ty0, tx1, ty1], radius=6, fill=_SURFACE, outline=_BORDER, width=1)
    draw.text((tx0 + 10, ty0 + 8), "TACTICAL TELEMETRY", fill=_MUTED, font=f_hdr)

    info_rows: list[tuple[str, str]] = []
    if center_mgrs:
        info_rows.append(("MGRS", center_mgrs))
    if center_lat is not None and center_lon is not None:
        info_rows.append(("WGS84", f"{float(center_lat):.4f}, {float(center_lon):.4f}"))
    if utm_zone:
        info_rows.append(("UTM", f"{utm_zone}{utm_hemi} (EPSG:{utm_epsg})"))
    if sq_ids:
        info_rows.append(("100km", ", ".join(sq_ids[:4])))
    if isinstance(viewshed, dict):
        vis_km2 = viewshed.get("visible_area_km2")
        obs_el = viewshed.get("observer_elev_m")
        max_d = viewshed.get("max_distance_m")
        if vis_km2 is not None:
            info_rows.append(("Visible", f"{float(vis_km2):.2f} km²"))
        if obs_el is not None:
            info_rows.append(("Obs Elev", f"{float(obs_el):.0f} m"))
        if max_d is not None:
            info_rows.append(("Radius", f"{float(max_d) / 1000.0:.1f} km"))
    elif gw_km > 0 and gh_km > 0:
        info_rows.append(("Ground", f"{gw_km:.2f}×{gh_km:.2f} km"))
        if grid_spacing_mm > 0:
            info_rows.append(("1km Grid", f"{grid_spacing_mm:.1f} mm"))

    ry = ty0 + 28
    for label, value in info_rows[:7]:
        draw.text((tx0 + 10, ry), label, fill=_DIM, font=f_small)
        val_clean = _truncate(value, 22)
        draw.text((tx0 + 10, ry + 10), val_clean, fill=_INK, font=f_mono)
        ry += 24
        if ry > ty1 - 18:
            break

    # Footer strip
    draw.line([(1, 420), (W - 2, 420)], fill=_BORDER, width=1)
    draw.rectangle([1, 421, W - 2, H - 2], fill=_HEADER_BG)
    footer_left = "True-Scale Local UTM · 1 km MGRS Grid · WMM2025 Magnetic Declination"
    draw.text((14, 434), footer_left, fill=_MUTED, font=f_mono)
    footer_right = "GridForge"
    fw, _ = _text_size(draw, footer_right, f_badge)
    draw.text((W - 14 - fw, 434), footer_right, fill=_AMBER, font=f_badge)

    return _encode_png(img)


# ---------------------------------------------------------------------------
# 3. Route / Ruler / Strip Atlas Card (720 x 400)
# ---------------------------------------------------------------------------


def render_route_card_png(
    route_or_ruler_dict: dict[str, Any],
    *,
    title: str | None = None,
) -> bytes:
    """Render a crisp 720x400 dark-tactical PNG schematic of a route, ruler, or strip atlas plan."""
    W, H = 720, 400
    img = Image.new("RGBA", (W, H), (*_BG, 255))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    odraw = ImageDraw.Draw(overlay)

    f_title = _load_font(14, bold=True)
    f_badge = _load_font(10, bold=True, mono=True)
    f_mono = _load_font(10, mono=True)
    f_small = _load_font(9, mono=True)
    f_hdr = _load_font(10, bold=True)

    is_strip = "sheets" in route_or_ruler_dict and "route_geojson" in route_or_ruler_dict
    waypoints_list: list[dict[str, Any]] = list(route_or_ruler_dict.get("waypoints") or [])
    legs_list: list[dict[str, Any]] = list(route_or_ruler_dict.get("legs") or [])
    segments_list: list[dict[str, Any]] = list(route_or_ruler_dict.get("segments") or [])
    raw_points: list[Any] = list(route_or_ruler_dict.get("points") or [])
    sheets_list: list[dict[str, Any]] = list(route_or_ruler_dict.get("sheets") or [])

    if is_strip:
        total_km = float(route_or_ruler_dict.get("route_distance_km") or 0.0)
    elif "total_distance_km" in route_or_ruler_dict:
        total_km = float(route_or_ruler_dict.get("total_distance_km") or 0.0)
    else:
        total_km = float(route_or_ruler_dict.get("total_distance_m") or 0.0) / 1000.0
    total_mi = total_km * 0.621371

    utm_epsg = route_or_ruler_dict.get("utm_epsg")

    # Outer border + header
    draw.rectangle([0, 0, W - 1, H - 1], outline=_BORDER, width=1)
    draw.rectangle([1, 1, W - 2, 42], fill=_HEADER_BG)
    draw.line([(1, 42), (W - 2, 42)], fill=_BORDER, width=1)

    default_title = (
        f"STRIP ATLAS PLAN · {len(sheets_list)} SHEETS"
        if is_strip
        else "TACTICAL ROUTE CARD"
    )
    draw.text((14, 12), _truncate(title or default_title, 44), fill=_INK, font=f_title)
    dist_badge = f"{total_km:.2f} km ({total_mi:.2f} mi)"
    b = _draw_pill(draw, W - 12, 10, dist_badge, bg=(45, 34, 14), fg=_AMBER, border=_AMBER, font=f_badge, align_right=True)
    if utm_epsg:
        _draw_pill(draw, b[0] - 6, 10, f"EPSG:{utm_epsg}", bg=_SURFACE_ALT, fg=_CYAN, border=_BORDER, font=f_badge, align_right=True)

    # Left map schematic panel
    map_box = (14, 54, 412, 354)
    mx0, my0, mx1, my1 = map_box
    draw.rounded_rectangle([mx0, my0, mx1, my1], radius=6, fill=_SURFACE, outline=_BORDER, width=1)

    # Gather route coordinates (lon, lat) and labels
    route_coords: list[tuple[float, float]] = []
    markers: list[tuple[float, float, str, str]] = []  # lon, lat, code, category

    if is_strip:
        route_coords = _collect_geojson_coords(route_or_ruler_dict.get("route_geojson"))
    elif waypoints_list:
        for idx, wp in enumerate(waypoints_list, start=1):
            if wp.get("lat") is not None and wp.get("lon") is not None:
                lon, lat = float(wp["lon"]), float(wp["lat"])
                route_coords.append((lon, lat))
                markers.append(
                    (
                        lon,
                        lat,
                        str(wp.get("code") or f"WP{idx}"),
                        str(wp.get("category") or "waypoint").lower(),
                    )
                )
    elif raw_points:
        for idx, pt in enumerate(raw_points, start=1):
            if isinstance(pt, dict) and pt.get("lat") is not None and pt.get("lon") is not None:
                lon, lat = float(pt["lon"]), float(pt["lat"])
            elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                lat, lon = float(pt[0]), float(pt[1])
            else:
                continue
            route_coords.append((lon, lat))
            markers.append((lon, lat, f"P{idx}", "waypoint"))

    all_coords = list(route_coords)
    if is_strip:
        all_coords.extend(_collect_geojson_coords(route_or_ruler_dict.get("footprint_geojson")))

    proj = _make_geo_projector(all_coords, (mx0 + 18, my0 + 18, mx1 - 18, my1 - 18), pad_frac=0.08)

    # Strip sheet polygons if present
    strip_polys: list[tuple[str, list[tuple[float, float]]]] = []
    if is_strip:
        for feat in _iter_geojson_features(route_or_ruler_dict.get("footprint_geojson")):
            geom = feat.get("geometry") or {}
            props = feat.get("properties") or {}
            if geom.get("type") == "Polygon":
                rings = geom.get("coordinates") or []
                if rings and rings[0]:
                    pts = [proj(float(c[0]), float(c[1])) for c in rings[0] if len(c) >= 2]
                    if len(pts) >= 3:
                        odraw.polygon(pts, fill=(*_CYAN, 26))
                        strip_polys.append((str(props.get("sheet_id") or "S"), pts))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # Subtle background crosshair grid inside map panel
    for i in range(1, 5):
        gx = mx0 + (mx1 - mx0) * i / 5.0
        gy = my0 + (my1 - my0) * i / 5.0
        draw.line([(gx, my0 + 1), (gx, my1 - 1)], fill=_GRID, width=1)
        draw.line([(mx0 + 1, gy), (mx1 - 1, gy)], fill=_GRID, width=1)

    for sid, pts in strip_polys:
        draw.line(pts, fill=_CYAN, width=1)
        uniq = pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else pts
        cx = sum(p[0] for p in uniq) / max(1, len(uniq))
        cy = sum(p[1] for p in uniq) / max(1, len(uniq))
        draw.text((cx - 8, cy - 6), sid, fill=_CYAN, font=f_badge)

    if len(route_coords) >= 2:
        r_pts = [proj(lo, la) for lo, la in route_coords]
        draw.line(r_pts, fill=_ORANGE, width=3)
        # Start and end dots for strip routes
        if not markers:
            for pt, col, lbl in (
                (r_pts[0], _EMERALD, "START"),
                (r_pts[-1], _CRIMSON, "END"),
            ):
                draw.ellipse([pt[0] - 5, pt[1] - 5, pt[0] + 5, pt[1] + 5], fill=col, outline=_BG, width=1)
                draw.text((pt[0] + 7, pt[1] - 6), lbl, fill=col, font=f_small)

    for idx, (lo, la, code, cat) in enumerate(markers, start=1):
        px, py = proj(lo, la)
        col = _WP_COLORS.get(cat, _AMBER)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=col, outline=_BG, width=1)
        num = str(idx)
        nw, nh = _text_size(draw, num, f_small)
        draw.text((px - nw / 2, py - nh / 2 - 1), num, fill=_BG, font=f_small)
        draw.text((px + 9, py - 5), code, fill=_INK, font=f_small)

    # Right table panel
    tx0, ty0, tx1, ty1 = 424, 54, 706, 354
    draw.rounded_rectangle([tx0, ty0, tx1, ty1], radius=6, fill=_SURFACE, outline=_BORDER, width=1)

    # Check if we have elevation values to show a mini sparkline at bottom of right panel
    elev_vals: list[float] = []
    if segments_list:
        for seg in segments_list:
            if seg.get("elev_start_m") is not None:
                elev_vals.append(float(seg["elev_start_m"]))
        if segments_list[-1].get("elev_end_m") is not None:
            elev_vals.append(float(segments_list[-1]["elev_end_m"]))
    elif waypoints_list:
        elev_vals = [float(w["elev_m"]) for w in waypoints_list if w.get("elev_m") is not None]

    has_elev = len(elev_vals) >= 2
    table_bottom = ty1 - 74 if has_elev else ty1 - 10

    if is_strip:
        draw.text((tx0 + 10, ty0 + 8), "STRIP SHEETS (TRUE-SCALE UTM)", fill=_MUTED, font=f_hdr)
        draw.line([(tx0 + 8, ty0 + 24), (tx1 - 8, ty0 + 24)], fill=_BORDER, width=1)
        ry = ty0 + 30
        for sh in sheets_list[:10]:
            sid = str(sh.get("sheet_id") or "")
            smgrs = str(sh.get("center_mgrs_10m") or "")
            draw.text((tx0 + 10, ry), sid, fill=_CYAN, font=f_badge)
            draw.text((tx0 + 48, ry), _truncate(smgrs, 24), fill=_INK, font=f_mono)
            ry += 22
            if ry > table_bottom - 16:
                break
        if len(sheets_list) > 10:
            draw.text((tx0 + 10, ry), f"+ {len(sheets_list) - 10} more sheets", fill=_MUTED, font=f_small)
    elif legs_list:
        draw.text((tx0 + 10, ty0 + 8), "LEG      DIST    GRID   MAG   MILS", fill=_MUTED, font=f_small)
        draw.line([(tx0 + 8, ty0 + 23), (tx1 - 8, ty0 + 23)], fill=_BORDER, width=1)
        ry = ty0 + 29
        max_rows = max(1, (table_bottom - ry) // 21)
        for leg in legs_list[:max_rows]:
            pair = f"{leg.get('from_code', 'W1')}→{leg.get('to_code', 'W2')}"
            d_km = float(leg.get("leg_dist_km") or 0.0)
            g_az = float(leg.get("grid_az_deg") or 0.0)
            m_az = float(leg.get("mag_az_deg") or 0.0)
            mils = float(leg.get("mag_az_mils") or 0.0)
            row_txt = f"{pair:<7} {d_km:5.2f}k {g_az:5.1f}° {m_az:5.1f}° {mils:4.0f}"
            draw.text((tx0 + 10, ry), row_txt, fill=_INK, font=f_small)
            ry += 21
        if len(legs_list) > max_rows:
            draw.text((tx0 + 10, ry), f"+ {len(legs_list) - max_rows} more legs", fill=_MUTED, font=f_small)
    elif segments_list:
        draw.text((tx0 + 10, ty0 + 8), "SEG      DIST    GRID   MAG   MILS", fill=_MUTED, font=f_small)
        draw.line([(tx0 + 8, ty0 + 23), (tx1 - 8, ty0 + 23)], fill=_BORDER, width=1)
        ry = ty0 + 29
        max_rows = max(1, (table_bottom - ry) // 21)
        for seg in segments_list[:max_rows]:
            pair = f"P{int(seg.get('from_idx', 0)) + 1}→P{int(seg.get('to_idx', 1)) + 1}"
            d_km = float(seg.get("distance_m") or 0.0) / 1000.0
            g_az = float(seg.get("bearing_grid_deg") or 0.0)
            m_az = float(seg.get("bearing_mag_deg") or 0.0)
            mils = float(seg.get("bearing_mils") or 0.0)
            row_txt = f"{pair:<7} {d_km:5.2f}k {g_az:5.1f}° {m_az:5.1f}° {mils:4.0f}"
            draw.text((tx0 + 10, ry), row_txt, fill=_INK, font=f_small)
            ry += 21
        if len(segments_list) > max_rows:
            draw.text((tx0 + 10, ry), f"+ {len(segments_list) - max_rows} more segments", fill=_MUTED, font=f_small)
    else:
        draw.text((tx0 + 12, ty0 + 16), "No route legs recorded", fill=_MUTED, font=f_mono)

    # Mini elevation sparkline if elevation was sampled
    if has_elev:
        ex0, ey0, ex1, ey1 = tx0 + 10, ty1 - 62, tx1 - 10, ty1 - 10
        draw.line([(tx0 + 8, ey0 - 8), (tx1 - 8, ey0 - 8)], fill=_BORDER, width=1)
        emin, emax = min(elev_vals), max(elev_vals)
        draw.text(
            (ex0, ey0 - 6),
            f"ELEV {round(emin):,}m – {round(emax):,}m",
            fill=_TERRAIN_LINE,
            font=f_small,
        )
        span = max(emax - emin, 10.0)
        epts = [
            (
                ex0 + (ex1 - ex0) * i / max(1, len(elev_vals) - 1),
                ey1 - ((ev - emin) / span) * (ey1 - ey0 - 12),
            )
            for i, ev in enumerate(elev_vals)
        ]
        draw.line(epts, fill=_TERRAIN_LINE, width=2)

    # Footer bar
    draw.line([(1, 366), (W - 2, 366)], fill=_BORDER, width=1)
    draw.rectangle([1, 367, W - 2, H - 2], fill=_HEADER_BG)
    n_items = (
        f"{len(sheets_list)} strip sheets · 1:{int(route_or_ruler_dict.get('scale') or 25000):,}"
        if is_strip
        else f"{len(legs_list) or len(segments_list)} leg(s) · Geodesic WGS84 + WMM2025 Azimuths"
    )
    draw.text((14, 378), n_items, fill=_MUTED, font=f_mono)
    fw, _ = _text_size(draw, "GridForge", f_badge)
    draw.text((W - 14 - fw, 378), "GridForge", fill=_AMBER, font=f_badge)

    return _encode_png(img)
