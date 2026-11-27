import {
  escapeHtml,
  formatSignedDeg,
  formatSignedMils,
  type DeclinationData,
} from "./types.ts";

export interface CompassAngles {
  trueDeg: number;
  gridDeg: number;
  magDeg: number;
  gmDeg: number;
}

/**
 * Computes exaggerated-but-ordered visual angles (in degrees clockwise from North)
 * so small grid convergence or declination angles remain legible on a compact diagram.
 */
export function computeCompassAngles(declination: DeclinationData): CompassAngles {
  const rawMag = Number.isFinite(declination.magnetic_declination_deg)
    ? declination.magnetic_declination_deg
    : 0;
  const rawConv = Number.isFinite(declination.grid_convergence_deg)
    ? declination.grid_convergence_deg
    : 0;
  // Grid North relative to True North: positive convergence places GN east (+) or west (-)
  // Standard UTM convention: G-M = Magnetic - Convergence => GN is at +convergence relative to TN
  // when TN is at 0 and MN is at +magnetic_declination.
  const scaleVisual = (deg: number, maxDeg: number, minNonZero: number): number => {
    if (Math.abs(deg) < 0.05) return 0;
    const sign = deg >= 0 ? 1 : -1;
    // Mild non-linear expansion for small angles, clamped to maxDeg
    const mag = Math.min(maxDeg, Math.max(minNonZero, Math.abs(deg) * 1.45));
    return sign * mag;
  };

  let magDeg = scaleVisual(rawMag, 36, 15);
  let gridDeg = scaleVisual(rawConv, 22, 13);

  // Ensure GN and MN rays don't visually collide if they have the same sign and close angles
  if (Math.abs(magDeg - gridDeg) < 13 && Math.abs(rawMag - rawConv) >= 0.05) {
    const dir = rawMag >= rawConv ? 1 : -1;
    magDeg = Math.max(-38, Math.min(38, gridDeg + dir * 15));
  }

  return {
    trueDeg: 0,
    gridDeg,
    magDeg,
    gmDeg: Number.isFinite(declination.gm_angle_deg)
      ? declination.gm_angle_deg
      : rawMag - rawConv,
  };
}

function polarPoint(cx: number, cy: number, radius: number, angleDeg: number): [number, number] {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return [cx + radius * Math.cos(rad), cy + radius * Math.sin(rad)];
}

/**
 * Renders a crisp WMM2025 True / Grid / Magnetic North declination compass SVG
 * using CSS custom properties so it adapts seamlessly to light & dark themes.
 */
export function renderDeclinationCompassSvg(
  declination: DeclinationData,
  options: { compact?: boolean } = {}
): string {
  const compact = Boolean(options.compact);
  const w = compact ? 116 : 138;
  const h = compact ? 104 : 118;
  const cx = w / 2;
  const cy = h - 18;
  const rTrue = compact ? 64 : 74;
  const rGrid = compact ? 58 : 67;
  const rMag = compact ? 60 : 70;
  const rArc = compact ? 34 : 40;

  const { gridDeg, magDeg } = computeCompassAngles(declination);

  const [tx, ty] = polarPoint(cx, cy, rTrue, 0);
  const [gx, gy] = polarPoint(cx, cy, rGrid, gridDeg);
  const [mx, my] = polarPoint(cx, cy, rMag, magDeg);

  // Half-arrowhead barb for Magnetic North ray (NATO standard)
  const barbAngle = magDeg >= 0 ? magDeg + 150 : magDeg - 150;
  const [bx, by] = polarPoint(mx, my, 8.5, barbAngle);

  // Arc between GN and MN
  const [ax1, ay1] = polarPoint(cx, cy, rArc, gridDeg);
  const [ax2, ay2] = polarPoint(cx, cy, rArc, magDeg);
  const sweepFlag = magDeg >= gridDeg ? 1 : 0;
  const showArc = Math.abs(magDeg - gridDeg) >= 4;

  // Label placements pushed cleanly outside ray tips
  const [glx, gly] = polarPoint(cx, cy, rGrid + 11, gridDeg);
  const [mlx, mly] = polarPoint(cx, cy, rMag + 12, magDeg);

  const gmShort = formatSignedDeg(declination.gm_angle_deg, 1);
  const aria = `WMM2025 declination diagram: Magnetic ${formatSignedDeg(declination.magnetic_declination_deg)}, Grid-Magnetic ${gmShort} (${formatSignedMils(declination.gm_angle_mils)})`;

  return `
    <svg class="gf-compass-svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${escapeHtml(aria)}">
      <defs>
        <radialGradient id="gfCompassGlow" cx="50%" cy="82%" r="72%">
          <stop offset="0%" stop-color="var(--gf-accent-cyan)" stop-opacity="0.14" />
          <stop offset="100%" stop-color="var(--gf-accent-cyan)" stop-opacity="0" />
        </radialGradient>
      </defs>
      <rect x="0.5" y="0.5" width="${w - 1}" height="${h - 1}" rx="8" fill="var(--gf-surface-inset)" stroke="var(--gf-border-subtle)" />
      <rect x="1" y="1" width="${w - 2}" height="${h - 2}" rx="7.5" fill="url(#gfCompassGlow)" />

      <!-- Subtle compass reference arc -->
      <path d="M ${(cx - rArc - 10).toFixed(1)} ${(cy - 24).toFixed(1)} A ${rArc + 14} ${rArc + 14} 0 0 1 ${(cx + rArc + 10).toFixed(1)} ${(cy - 24).toFixed(1)}" fill="none" stroke="var(--gf-border-subtle)" stroke-width="0.8" stroke-dasharray="2 3" />

      ${
        showArc
          ? `<path d="M ${ax1.toFixed(1)} ${ay1.toFixed(1)} A ${rArc} ${rArc} 0 0 ${sweepFlag} ${ax2.toFixed(1)} ${ay2.toFixed(1)}" fill="none" stroke="var(--gf-accent-amber)" stroke-width="1.6" stroke-linecap="round" opacity="0.9" />`
          : ""
      }

      <!-- True North (Star) -->
      <line x1="${cx.toFixed(1)}" y1="${cy.toFixed(1)}" x2="${tx.toFixed(1)}" y2="${ty.toFixed(1)}" stroke="var(--gf-text-secondary)" stroke-width="1.35" stroke-linecap="round" />
      <text x="${tx.toFixed(1)}" y="${(ty - 4).toFixed(1)}" text-anchor="middle" fill="var(--gf-text-primary)" font-size="10.5" font-weight="700" font-family="var(--gf-font-sans)">★</text>

      <!-- Grid North (GN) -->
      <line x1="${cx.toFixed(1)}" y1="${cy.toFixed(1)}" x2="${gx.toFixed(1)}" y2="${gy.toFixed(1)}" stroke="var(--gf-accent-cyan)" stroke-width="1.5" stroke-linecap="round" />
      <text x="${glx.toFixed(1)}" y="${(gly + 2.5).toFixed(1)}" text-anchor="middle" fill="var(--gf-accent-cyan)" font-size="8.5" font-weight="700" font-family="var(--gf-font-mono)">GN</text>

      <!-- Magnetic North (MN) -->
      <line x1="${cx.toFixed(1)}" y1="${cy.toFixed(1)}" x2="${mx.toFixed(1)}" y2="${my.toFixed(1)}" stroke="var(--gf-accent-amber)" stroke-width="1.8" stroke-linecap="round" />
      <line x1="${mx.toFixed(1)}" y1="${my.toFixed(1)}" x2="${bx.toFixed(1)}" y2="${by.toFixed(1)}" stroke="var(--gf-accent-amber)" stroke-width="1.8" stroke-linecap="round" />
      <text x="${mlx.toFixed(1)}" y="${(mly + 2.5).toFixed(1)}" text-anchor="middle" fill="var(--gf-accent-amber)" font-size="8.5" font-weight="700" font-family="var(--gf-font-mono)">MN</text>

      <!-- Pivot & G-M footer label -->
      <circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="2.6" fill="var(--gf-bg-card)" stroke="var(--gf-text-primary)" stroke-width="1.3" />
      <text x="${cx.toFixed(1)}" y="${(h - 4).toFixed(1)}" text-anchor="middle" fill="var(--gf-text-muted)" font-size="8" font-family="var(--gf-font-mono)">G-M ${escapeHtml(gmShort)}</text>
    </svg>
  `.trim();
}
