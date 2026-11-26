import { escapeHtml } from "../presets";
import type { RFLinkProfileResponse } from "../types";

/**
 * Render a crisp dark-mode inline SVG Fresnel zone & terrain clearance profile
 * for a point-to-point RF link.
 */
export function renderRFLinkSvg(profile: RFLinkProfileResponse): string {
  const samples = profile.samples;
  if (!samples || samples.length < 2) {
    return `<div class="gf-wp-empty">No elevation samples available.</div>`;
  }

  const width = 316;
  const height = 114;
  const padL = 34;
  const padR = 8;
  const padT = 10;
  const padB = 20;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  const maxDist = Math.max(profile.distance_km, samples[samples.length - 1]?.dist_km ?? 1, 0.01);

  let minY = Infinity;
  let maxY = -Infinity;
  for (const s of samples) {
    minY = Math.min(minY, s.terrain_m, s.fresnel_lo_m, s.los_m);
    maxY = Math.max(maxY, s.terrain_m, s.fresnel_hi_m, s.los_m);
  }
  if (!Number.isFinite(minY) || !Number.isFinite(maxY)) {
    minY = 0;
    maxY = 100;
  }
  const spanY = Math.max(maxY - minY, 20);
  minY -= spanY * 0.08;
  maxY += spanY * 0.1;

  const xOf = (d: number) => padL + (d / maxDist) * plotW;
  const yOf = (m: number) => padT + (1 - (m - minY) / (maxY - minY)) * plotH;

  const terrainPts = samples
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.terrain_m).toFixed(1)}`)
    .join(" ");
  const terrainArea = `${padL},${(padT + plotH).toFixed(1)} ${terrainPts} ${(padL + plotW).toFixed(1)},${(padT + plotH).toFixed(1)}`;

  const losPts = samples
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.los_m).toFixed(1)}`)
    .join(" ");

  const fresnelUpper = samples.map(
    (s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.fresnel_hi_m).toFixed(1)}`
  );
  const fresnelLowerRev = [...samples]
    .reverse()
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.fresnel_lo_m).toFixed(1)}`);
  const fresnelPoly = [...fresnelUpper, ...fresnelLowerRev].join(" ");

  const f60Pts = samples
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.fresnel60_lo_m).toFixed(1)}`)
    .join(" ");

  const statusColor =
    profile.status === "CLEAR"
      ? "#10B981"
      : profile.status === "MARGINAL"
        ? "#F59E0B"
        : "#EF4444";

  const midElev = Math.round((minY + maxY) / 2);

  return `
    <svg class="gf-rf-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="RF Fresnel clearance profile (${escapeHtml(profile.status)})">
      <rect x="0" y="0" width="${width}" height="${height}" rx="4" fill="#0B1015" />
      <line x1="${padL}" y1="${padT}" x2="${padL + plotW}" y2="${padT}" stroke="#1E293B" stroke-width="0.7" />
      <line x1="${padL}" y1="${(padT + plotH / 2).toFixed(1)}" x2="${padL + plotW}" y2="${(padT + plotH / 2).toFixed(1)}" stroke="#1E293B" stroke-width="0.7" stroke-dasharray="2 2" />
      <line x1="${padL}" y1="${padT + plotH}" x2="${padL + plotW}" y2="${padT + plotH}" stroke="#334155" stroke-width="0.9" />

      <polygon points="${fresnelPoly}" fill="${statusColor}" fill-opacity="0.16" stroke="${statusColor}" stroke-opacity="0.55" stroke-width="0.8" />
      <polyline points="${f60Pts}" fill="none" stroke="#FBBF24" stroke-width="0.8" stroke-dasharray="3 2" opacity="0.75" />
      <polygon points="${terrainArea}" fill="#334155" fill-opacity="0.78" />
      <polyline points="${terrainPts}" fill="none" stroke="#94A3B8" stroke-width="1.2" />
      <polyline points="${losPts}" fill="none" stroke="${statusColor}" stroke-width="1.4" stroke-dasharray="4 2" />

      <text x="${padL - 4}" y="${padT + 4}" text-anchor="end" fill="#64748B" font-size="8" font-family="monospace">${Math.round(maxY)}m</text>
      <text x="${padL - 4}" y="${(padT + plotH / 2 + 3).toFixed(1)}" text-anchor="end" fill="#64748B" font-size="8" font-family="monospace">${midElev}m</text>
      <text x="${padL - 4}" y="${padT + plotH}" text-anchor="end" fill="#64748B" font-size="8" font-family="monospace">${Math.round(minY)}m</text>

      <text x="${padL}" y="${height - 5}" text-anchor="start" fill="#94A3B8" font-size="8" font-family="monospace">A · 0 km</text>
      <text x="${padL + plotW / 2}" y="${height - 5}" text-anchor="middle" fill="#64748B" font-size="8" font-family="monospace">${(maxDist / 2).toFixed(1)} km</text>
      <text x="${padL + plotW}" y="${height - 5}" text-anchor="end" fill="#94A3B8" font-size="8" font-family="monospace">B · ${maxDist.toFixed(1)} km</text>
    </svg>
  `;
}
