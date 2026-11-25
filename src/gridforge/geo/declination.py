"""WMM2025 magnetic declination, UTM grid convergence, and military collar SVG diagrams."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import pyproj
from pygeomag import GeoMag

from gridforge.config import DeclinationInfo, MGRSGridMeta, SheetLayout

DEG_TO_NATO_MILS: float = 6400.0 / 360.0
WMM2025_MIN_YEAR: float = 2025.0
WMM2025_MAX_YEAR: float = 2030.0


def _wrap_180(deg: float) -> float:
    """Wrap an angle in degrees to (-180.0, 180.0]."""
    wrapped = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def _parse_date_to_decimal_year(date_iso: str | None) -> float:
    """Convert an ISO-8601 date string (or today UTC) into exact decimal year."""
    if date_iso is None:
        target = datetime.now(timezone.utc).date()
    else:
        raw = date_iso.strip()
        try:
            target = date.fromisoformat(raw[:10])
        except ValueError as exc:
            raise ValueError(
                f"Invalid ISO date '{date_iso}'. Expected YYYY-MM-DD."
            ) from exc

    year_start = date(target.year, 1, 1)
    next_year_start = date(target.year + 1, 1, 1)
    days_in_year = (next_year_start - year_start).days
    day_of_year = (target - year_start).days
    return target.year + (day_of_year / days_in_year)


def _read_cof_metadata(cof_path: Path) -> tuple[float, float, str]:
    """Read (min_year, max_year, model_name) from the header line of a NOAA WMM .COF file."""
    first_line = cof_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    parts = first_line.split()
    epoch = float(parts[0])
    model_name = parts[1] if len(parts) > 1 else "WMM-CUSTOM"
    return (epoch, epoch + 5.0, model_name)


def compute_declination(
    layout: SheetLayout,
    elevation_m: float = 0.0,
) -> DeclinationInfo:
    """Compute WMM2025 magnetic declination, UTM grid convergence, and TC 3-25.26 G-M angle."""
    decimal_year = _parse_date_to_decimal_year(layout.spec.date_iso)

    cof_path = layout.spec.wmm_cof_path
    if cof_path is not None:
        if not cof_path.exists():
            raise FileNotFoundError(f"Custom WMM .COF file not found: '{cof_path}'")
        min_year, max_year, model_name = _read_cof_metadata(cof_path)
        geo_mag = GeoMag(coefficients_file=str(cof_path))
    else:
        min_year, max_year, model_name = (
            WMM2025_MIN_YEAR,
            WMM2025_MAX_YEAR,
            "WMM2025",
        )
        geo_mag = GeoMag()

    if not (min_year <= decimal_year <= max_year):
        raise ValueError(
            f"Decimal year {decimal_year:.3f} is outside {model_name} valid range "
            f"[{min_year:.1f}, {max_year:.1f}]. Provide a custom WMM .COF file via wmm_cof_path."
        )

    lat = layout.spec.center_lat
    lon = layout.spec.center_lon
    # CRITICAL: pygeomag expects altitude in kilometers.
    alt_km = float(elevation_m) / 1000.0

    mag_decl = float(geo_mag.calculate(glat=lat, glon=lon, alt=alt_km, time=decimal_year).d)

    # Centered finite-difference annual secular variation clamped to model lifespan.
    t1 = max(min_year, decimal_year - 0.5)
    t2 = min(max_year, decimal_year + 0.5)
    d1 = float(geo_mag.calculate(glat=lat, glon=lon, alt=alt_km, time=t1).d)
    d2 = float(geo_mag.calculate(glat=lat, glon=lon, alt=alt_km, time=t2).d)
    annual_change = _wrap_180(d2 - d1) / (t2 - t1)

    # Grid convergence via PROJ (lon first! + = Grid North clockwise/East of True North).
    proj = pyproj.Proj(f"EPSG:{layout.utm_epsg}")
    gamma = float(proj.get_factors(lon, lat).meridian_convergence)

    # Hemisphere-invariant TC 3-25.26 G-M angle (+ = Easterly G-M, - = Westerly G-M).
    gm_angle_deg = _wrap_180(mag_decl - gamma)
    gm_angle_mils = gm_angle_deg * DEG_TO_NATO_MILS
    grid_convergence_mils = gamma * DEG_TO_NATO_MILS

    return DeclinationInfo(
        decimal_year=decimal_year,
        magnetic_declination_deg=mag_decl,
        annual_change_deg=annual_change,
        grid_convergence_deg=gamma,
        gm_angle_deg=gm_angle_deg,
        gm_angle_mils=gm_angle_mils,
        grid_convergence_mils=grid_convergence_mils,
        model_name=model_name,
    )


def _format_deg_min(angle_deg: float) -> str:
    """Format an angle magnitude in degrees and integer arcminutes, e.g. 1°29'."""
    total_min = round(abs(angle_deg) * 60.0)
    deg = total_min // 60
    mins = total_min % 60
    return f"{deg}°{mins:02d}'"


def _compute_visual_prong_angles(tn_deg: float, mn_deg: float) -> tuple[float, float]:
    """Exaggerate small prong angles to ensure >=21°–25° visual separation without reordering."""
    min_tn_sep = 21.0
    min_mn_sep = 25.0
    min_same_side_gap = 21.0
    max_angle = 43.0

    def _exaggerate_single(val: float, min_sep: float) -> float:
        if abs(val) < 0.02:
            return 0.0
        sign = 1.0 if val > 0 else -1.0
        mag = max(min_sep, min(max_angle, abs(val) * 1.65))
        return sign * mag

    tn_vis = _exaggerate_single(tn_deg, min_tn_sep)
    mn_vis = _exaggerate_single(mn_deg, min_mn_sep)

    # When both TN and MN lie on the same side of GN, preserve ordering with generous gap.
    if tn_vis != 0.0 and mn_vis != 0.0 and (tn_vis > 0) == (mn_vis > 0):
        sign = 1.0 if tn_vis > 0 else -1.0
        if abs(mn_vis - tn_vis) < min_same_side_gap:
            if abs(mn_deg) >= abs(tn_deg):
                tn_vis = sign * min_same_side_gap
                mn_vis = sign * min(max_angle, 2.0 * min_same_side_gap)
            else:
                mn_vis = sign * min_same_side_gap
                tn_vis = sign * min(max_angle, 2.0 * min_same_side_gap)

    return (tn_vis, mn_vis)


def _polar_point(cx: float, cy: float, radius: float, angle_deg: float) -> tuple[float, float]:
    """Convert clockwise-from-North angle (deg) to SVG (x, y) coordinates."""
    rad = math.radians(angle_deg)
    return (cx + radius * math.sin(rad), cy - radius * math.cos(rad))


def _svg_arc_path(cx: float, cy: float, radius: float, a1_deg: float, a2_deg: float) -> str:
    """Build an SVG path `d` string for a circular arc from a1_deg to a2_deg (cw from North)."""
    x1, y1 = _polar_point(cx, cy, radius, a1_deg)
    x2, y2 = _polar_point(cx, cy, radius, a2_deg)
    delta = a2_deg - a1_deg
    sweep = 1 if delta >= 0 else 0
    large_arc = 1 if abs(delta) > 180.0 else 0
    return f"M {x1:.2f} {y1:.2f} A {radius:.2f} {radius:.2f} 0 {large_arc} {sweep} {x2:.2f} {y2:.2f}"


def _star_polygon_points(cx: float, cy: float, outer_r: float = 4.6, inner_r: float = 1.9) -> str:
    """Return SVG polygon points for a crisp 5-point True North star."""
    pts: list[str] = []
    for i in range(10):
        r = outer_r if i % 2 == 0 else inner_r
        angle = math.radians(-90.0 + i * 36.0)
        px = cx + r * math.cos(angle)
        py = cy + r * math.sin(angle)
        pts.append(f"{px:.2f},{py:.2f}")
    return " ".join(pts)


def _half_barb_points(
    tip_x: float,
    tip_y: float,
    angle_deg: float,
    side: int = 1,
    length: float = 9.0,
    width: float = 4.2,
) -> str:
    """Return SVG polygon points for a military Magnetic North half-barb arrowhead."""
    rad = math.radians(angle_deg)
    # Unit vector along prong (pointing toward tip) and perpendicular outward vector.
    ux, uy = math.sin(rad), -math.cos(rad)
    px, py = side * math.cos(rad), side * math.sin(rad)

    base_x = tip_x - length * ux
    base_y = tip_y - length * uy
    wing_x = base_x + width * px
    wing_y = base_y + width * py
    return f"{tip_x:.2f},{tip_y:.2f} {wing_x:.2f},{wing_y:.2f} {base_x:.2f},{base_y:.2f}"


def render_declination_svg(
    info: DeclinationInfo,
    output_svg: Path,
    bw: bool = False,
) -> Path:
    """Render a crisp, self-contained TC 3-25.26 declination diagram SVG for collar embedding."""
    output_svg.parent.mkdir(parents=True, exist_ok=True)

    ink = "#000000" if bw else "#111111"
    muted = "#222222" if bw else "#444444"
    rule_stroke = "#111111" if bw else "#888888"
    bg_fill = "#FFFFFF"
    font = "Liberation Sans, DejaVu Sans, sans-serif"

    # Diagram geometry (left panel).
    cx, cy = 57.0, 94.0
    gn_len = 60.0
    tn_len = 60.0
    mn_len = 58.0

    # Geometric prong angles (GN=0°, clockwise positive): TN = -gamma, MN = G-M = D - gamma.
    tn_true = -info.grid_convergence_deg
    mn_true = info.gm_angle_deg
    tn_vis, mn_vis = _compute_visual_prong_angles(tn_true, mn_true)

    gn_tip = _polar_point(cx, cy, gn_len, 0.0)
    tn_tip = _polar_point(cx, cy, tn_len, tn_vis)
    mn_tip = _polar_point(cx, cy, mn_len, mn_vis)

    # Half-barb points outward away from GN (right if mn_vis >= 0, left if mn_vis < 0).
    barb_side = 1 if mn_vis >= 0.0 else -1
    mn_barb = _half_barb_points(mn_tip[0], mn_tip[1], mn_vis, side=barb_side)

    # Labels positioned cleanly beyond prong tips.
    tn_lbl_pt = _polar_point(cx, cy, tn_len + 12.0, tn_vis)
    mn_lbl_pt = _polar_point(cx, cy, mn_len + 11.0, mn_vis)

    # Arcs between GN and TN / MN.
    arcs_svg: list[str] = []
    if abs(tn_vis) >= 1.0:
        arcs_svg.append(
            f'<path d="{_svg_arc_path(cx, cy, 23.0, 0.0, tn_vis)}" '
            f'fill="none" stroke="{muted}" stroke-width="0.85" stroke-dasharray="2,1.5"/>'
        )
    if abs(mn_vis) >= 1.0:
        arcs_svg.append(
            f'<path d="{_svg_arc_path(cx, cy, 37.0, 0.0, mn_vis)}" '
            f'fill="none" stroke="{ink}" stroke-width="0.95"/>'
        )
        # Place G-M label centered in the widest unobstructed open sector along the G-M arc.
        if (tn_vis > 0) == (mn_vis > 0) and 0.0 < abs(tn_vis) < abs(mn_vis):
            gm_lbl_angle = (tn_vis + mn_vis) * 0.5
        else:
            gm_lbl_angle = mn_vis * 0.5
        gm_mid_pt = _polar_point(cx, cy, 44.5, gm_lbl_angle)
        # White SVG 1.2 Tiny-compatible stroke halo followed by crisp ink fill.
        arcs_svg.append(
            f'<text x="{gm_mid_pt[0]:.2f}" y="{gm_mid_pt[1]:.2f}" '
            f'font-family="{font}" font-size="5.5" font-weight="bold" '
            f'text-anchor="middle" fill="{bg_fill}" stroke="{bg_fill}" '
            f'stroke-width="2.4" stroke-linejoin="round">G-M</text>'
        )
        arcs_svg.append(
            f'<text x="{gm_mid_pt[0]:.2f}" y="{gm_mid_pt[1]:.2f}" '
            f'font-family="{font}" font-size="5.5" font-weight="bold" '
            f'text-anchor="middle" fill="{ink}">G-M</text>'
        )

    # Formatted numeric readouts.
    gm_dir = "E" if info.gm_angle_deg >= 0.0 else "W"
    gm_deg_abs = abs(info.gm_angle_deg)
    gm_mils_abs = round(abs(info.gm_angle_mils))

    conv_dir = "E" if info.grid_convergence_deg >= 0.0 else "W"
    conv_dm = _format_deg_min(info.grid_convergence_deg)
    conv_mils_abs = round(abs(info.grid_convergence_mils))

    decl_dir = "E" if info.magnetic_declination_deg >= 0.0 else "W"
    decl_abs = abs(info.magnetic_declination_deg)
    annual_arcmin = info.annual_change_deg * 60.0
    annual_str = f"{annual_arcmin:+.1f}'/yr"

    # TC 3-25.26 Azimuth Conversion note.
    if info.gm_angle_deg >= 0.0:
        g_to_m_op = "SUBTRACT"
        m_to_g_op = "ADD"
    else:
        g_to_m_op = "ADD"
        m_to_g_op = "SUBTRACT"

    conv_line_1 = f"Grid → Mag: {g_to_m_op} {gm_deg_abs:.1f}° ({gm_mils_abs} mils)"
    conv_line_2 = f"Mag → Grid: {m_to_g_op} {gm_deg_abs:.1f}° ({gm_mils_abs} mils)"

    header_sub = f"{info.model_name} ({info.decimal_year:.1f})"

    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="225" height="148" viewBox="0 0 225 148">',
        f'  <rect x="0.75" y="0.75" width="223.5" height="146.5" rx="1.5" '
        f'fill="{bg_fill}" stroke="{ink}" stroke-width="1.0"/>',
        # Left panel: North prongs
        f'  <line x1="{cx:.2f}" y1="{cy:.2f}" x2="{gn_tip[0]:.2f}" y2="{gn_tip[1]:.2f}" '
        f'stroke="{ink}" stroke-width="1.35"/>',
        f'  <rect x="{gn_tip[0] - 2.3:.2f}" y="{gn_tip[1] - 2.3:.2f}" width="4.6" height="4.6" '
        f'fill="{bg_fill}" stroke="{ink}" stroke-width="1.05"/>',
        f'  <text x="{gn_tip[0]:.2f}" y="{gn_tip[1] - 6.0:.2f}" font-family="{font}" '
        f'font-size="7.2" font-weight="bold" text-anchor="middle" fill="{ink}">GN</text>',
        # TN prong + 5-point star
        f'  <line x1="{cx:.2f}" y1="{cy:.2f}" x2="{tn_tip[0]:.2f}" y2="{tn_tip[1]:.2f}" '
        f'stroke="{ink}" stroke-width="1.05"/>',
        f'  <polygon points="{_star_polygon_points(tn_tip[0], tn_tip[1])}" fill="{ink}"/>',
        f'  <text x="{tn_lbl_pt[0]:.2f}" y="{tn_lbl_pt[1] + 2.2:.2f}" font-family="{font}" '
        f'font-size="6.8" font-weight="bold" text-anchor="middle" fill="{ink}">TN</text>',
        # MN prong + half-barb
        f'  <line x1="{cx:.2f}" y1="{cy:.2f}" x2="{mn_tip[0]:.2f}" y2="{mn_tip[1]:.2f}" '
        f'stroke="{ink}" stroke-width="1.15"/>',
        f'  <polygon points="{mn_barb}" fill="{ink}"/>',
        f'  <text x="{mn_lbl_pt[0]:.2f}" y="{mn_lbl_pt[1] + 2.2:.2f}" font-family="{font}" '
        f'font-size="6.8" font-weight="bold" text-anchor="middle" fill="{ink}">MN</text>',
        # Pivot dot
        f'  <circle cx="{cx:.2f}" cy="{cy:.2f}" r="1.8" fill="{ink}"/>',
        # Arcs
        *(f"  {arc}" for arc in arcs_svg),
        # Not to scale note
        f'  <text x="{cx:.2f}" y="105.0" font-family="{font}" font-size="5.5" '
        f'font-weight="bold" text-anchor="middle" fill="{muted}">DIAGRAM NOT TO SCALE</text>',
        # Vertical divider between diagram & data table
        f'  <line x1="114.0" y1="6.0" x2="114.0" y2="108.0" stroke="{rule_stroke}" stroke-width="0.6"/>',
        # Right panel: Numeric readout table
        f'  <text x="120.0" y="16.0" font-family="{font}" font-size="7.4" '
        f'font-weight="bold" fill="{ink}">DECLINATION</text>',
        f'  <text x="120.0" y="24.5" font-family="{font}" font-size="5.8" '
        f'fill="{muted}">{escape(header_sub)}</text>',
        f'  <line x1="120.0" y1="28.0" x2="219.0" y2="28.0" stroke="{rule_stroke}" stroke-width="0.5"/>',
        f'  <text x="120.0" y="38.5" font-family="{font}" font-size="5.8" '
        f'font-weight="bold" fill="{muted}">G-M ANGLE</text>',
        f'  <text x="120.0" y="48.5" font-family="{font}" font-size="7.5" '
        f'font-weight="bold" fill="{ink}">{escape(f"{gm_deg_abs:.1f}° {gm_dir} ({gm_mils_abs} mils)")}</text>',
        f'  <text x="120.0" y="61.0" font-family="{font}" font-size="5.8" '
        f'font-weight="bold" fill="{muted}">GRID CONVERGENCE</text>',
        f'  <text x="120.0" y="70.5" font-family="{font}" font-size="7.0" '
        f'fill="{ink}">{escape(f"{conv_dm} {conv_dir} ({conv_mils_abs} mils)")}</text>',
        f'  <text x="120.0" y="83.0" font-family="{font}" font-size="5.8" '
        f'font-weight="bold" fill="{muted}">MAGNETIC DECLINATION</text>',
        f'  <text x="120.0" y="92.5" font-family="{font}" font-size="7.0" '
        f'fill="{ink}">{escape(f"{decl_abs:.1f}° {decl_dir}")}</text>',
        f'  <text x="120.0" y="102.0" font-family="{font}" font-size="5.8" '
        f'fill="{muted}">{escape(f"Annual Change: {annual_str}")}</text>',
        # Bottom strip: TC 3-25.26 Azimuth Conversion
        f'  <line x1="6.0" y1="111.0" x2="219.0" y2="111.0" stroke="{ink}" stroke-width="0.75"/>',
        f'  <text x="112.5" y="120.5" font-family="{font}" font-size="6.0" '
        f'font-weight="bold" text-anchor="middle" fill="{ink}">AZIMUTH CONVERSION (TC 3-25.26)</text>',
        f'  <text x="112.5" y="131.0" font-family="{font}" font-size="6.4" '
        f'text-anchor="middle" fill="{ink}">{escape(conv_line_1)}</text>',
        f'  <text x="112.5" y="140.5" font-family="{font}" font-size="6.4" '
        f'text-anchor="middle" fill="{ink}">{escape(conv_line_2)}</text>',
        "</svg>",
    ]

    output_svg.write_text("\n".join(svg_lines) + "\n", encoding="utf-8")
    return output_svg


def render_grid_ref_box_svg(
    mgrs_meta: MGRSGridMeta,
    layout: SheetLayout,
    output_svg: Path,
) -> Path:
    """Render a military-style MGRS Grid Reference Box SVG for print collar embedding."""
    output_svg.parent.mkdir(parents=True, exist_ok=True)

    ink = "#111111"
    muted = "#444444"
    rule_stroke = "#888888"
    bg_fill = "#FFFFFF"
    font = "Liberation Sans, DejaVu Sans, sans-serif"

    squares_display = " / ".join(mgrs_meta.square_ids_100km)
    sq_font_size = 15.0 if len(mgrs_meta.square_ids_100km) <= 1 else (
        12.0 if len(mgrs_meta.square_ids_100km) == 2 else 9.5
    )

    # Also format a 100 m sample (first 3 digits of E and N) alongside 10 m sample.
    parts = mgrs_meta.center_mgrs_10m.split()
    if len(parts) == 4 and len(parts[2]) >= 3 and len(parts[3]) >= 3:
        sample_100m = f"{parts[0]} {parts[1]} {parts[2][:3]} {parts[3][:3]}"
    else:
        sample_100m = mgrs_meta.center_mgrs_10m

    proj_line = (
        f"UTM PROJECTION: EPSG:{layout.utm_epsg} "
        f"(ZONE {layout.utm_zone}{layout.utm_hemisphere}, WGS84)"
    )

    notes: list[str] = []
    if mgrs_meta.crosses_100km_boundary:
        notes.append("CROSSES 100 km SQUARE BOUNDARY")
    if mgrs_meta.crosses_utm_zone_boundary:
        notes.append("CROSSES UTM ZONE BOUNDARY")
    note_line = (
        " | ".join(notes)
        if notes
        else f"1 km GRID INTERVAL = {mgrs_meta.grid_spacing_mm:.2f} mm AT 1:{layout.spec.scale:,}"
    )

    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="215" height="148" viewBox="0 0 215 148">',
        f'  <rect x="0.75" y="0.75" width="213.5" height="146.5" rx="1.5" '
        f'fill="{bg_fill}" stroke="{ink}" stroke-width="1.0"/>',
        # Header banner
        f'  <rect x="1.0" y="1.0" width="213.0" height="19.0" fill="{ink}"/>',
        f'  <text x="107.5" y="13.5" font-family="{font}" font-size="7.6" '
        f'font-weight="bold" text-anchor="middle" fill="#FFFFFF">MGRS GRID REFERENCE BOX</text>',
        # Row 1: GZD (left) | 100,000m Square ID (right)
        f'  <line x1="107.5" y1="20.0" x2="107.5" y2="66.0" stroke="{ink}" stroke-width="0.75"/>',
        f'  <line x1="1.0" y1="66.0" x2="214.0" y2="66.0" stroke="{ink}" stroke-width="0.75"/>',
        f'  <text x="54.25" y="31.5" font-family="{font}" font-size="5.7" '
        f'font-weight="bold" text-anchor="middle" fill="{muted}">GRID ZONE DESIGNATION</text>',
        f'  <text x="54.25" y="54.0" font-family="{font}" font-size="16.0" '
        f'font-weight="bold" text-anchor="middle" fill="{ink}">{escape(mgrs_meta.gzd)}</text>',
        f'  <text x="160.75" y="31.5" font-family="{font}" font-size="5.7" '
        f'font-weight="bold" text-anchor="middle" fill="{muted}">100,000m SQUARE ID</text>',
        f'  <text x="160.75" y="54.0" font-family="{font}" font-size="{sq_font_size:.1f}" '
        f'font-weight="bold" text-anchor="middle" fill="{ink}">{escape(squares_display)}</text>',
        # Row 2: Sample Grid Reference
        f'  <text x="107.5" y="77.5" font-family="{font}" font-size="5.8" '
        f'font-weight="bold" text-anchor="middle" fill="{muted}">SAMPLE 10m / 100m GRID REF (CENTER)</text>',
        f'  <text x="107.5" y="92.0" font-family="{font}" font-size="10.0" '
        f'font-weight="bold" text-anchor="middle" fill="{ink}">{escape(mgrs_meta.center_mgrs_10m)}</text>',
        f'  <text x="107.5" y="103.5" font-family="{font}" font-size="6.4" '
        f'text-anchor="middle" fill="{muted}">{escape(f"100m REF: {sample_100m}  |  READ RIGHT, THEN UP")}</text>',
        # Row 3: UTM Projection & Grid Metadata
        f'  <line x1="1.0" y1="111.0" x2="214.0" y2="111.0" stroke="{rule_stroke}" stroke-width="0.65"/>',
        f'  <text x="107.5" y="124.5" font-family="{font}" font-size="6.2" '
        f'font-weight="bold" text-anchor="middle" fill="{ink}">{escape(proj_line)}</text>',
        f'  <text x="107.5" y="137.5" font-family="{font}" font-size="5.7" '
        f'text-anchor="middle" fill="{muted}">{escape(note_line)}</text>',
        "</svg>",
    ]

    output_svg.write_text("\n".join(svg_lines) + "\n", encoding="utf-8")
    return output_svg
