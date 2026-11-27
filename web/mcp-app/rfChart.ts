import { escapeHtml, type RFLinkProfileData, type RFLinkSample } from "./types.ts";

export interface RFChartLayout {
  width: number;
  height: number;
  padL: number;
  padR: number;
  padT: number;
  padB: number;
  plotW: number;
  plotH: number;
  maxDist: number;
  minY: number;
  maxY: number;
  minSampleIndex: number;
}

export function computeRFChartLayout(profile: RFLinkProfileData): RFChartLayout {
  const width = 680;
  const height = 228;
  const padL = 50;
  const padR = 16;
  const padT = 22;
  const padB = 30;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  const samples = profile.samples ?? [];
  const maxDist = Math.max(
    profile.distance_km || 0,
    samples.length > 0 ? samples[samples.length - 1]!.dist_km : 1,
    0.05
  );

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
  const span = Math.max(maxY - minY, 25);
  minY -= span * 0.08;
  maxY += span * 0.14;

  // Locate minimum clearance sample (prefer interior points over exact TX/RX base)
  let minSampleIndex = 0;
  if (samples.length > 2) {
    let bestVal = Infinity;
    for (let i = 1; i < samples.length - 1; i++) {
      const c = samples[i]!.clearance_m;
      if (c < bestVal) {
        bestVal = c;
        minSampleIndex = i;
      }
    }
  }

  return {
    width,
    height,
    padL,
    padR,
    padT,
    padB,
    plotW,
    plotH,
    maxDist,
    minY,
    maxY,
    minSampleIndex,
  };
}

export function findNearestRFSample(
  samples: RFLinkSample[],
  targetDistKm: number
): { sample: RFLinkSample; index: number } | null {
  if (!samples || samples.length === 0) return null;
  let bestIdx = 0;
  let bestErr = Math.abs(samples[0]!.dist_km - targetDistKm);
  for (let i = 1; i < samples.length; i++) {
    const err = Math.abs(samples[i]!.dist_km - targetDistKm);
    if (err < bestErr) {
      bestErr = err;
      bestIdx = i;
    }
  }
  return { sample: samples[bestIdx]!, index: bestIdx };
}

function statusTokenColor(status: RFLinkProfileData["status"]): string {
  if (status === "CLEAR") return "var(--gf-accent-emerald)";
  if (status === "MARGINAL") return "var(--gf-accent-amber)";
  return "var(--gf-accent-red)";
}

/**
 * Pure HTML+SVG markup builder for the curved-Earth 1st Fresnel terrain profile chart.
 */
export function buildRFChartMarkup(profile: RFLinkProfileData): string {
  const samples = profile.samples ?? [];
  if (samples.length < 2) {
    return `<div class="gf-empty-panel">No elevation samples returned for this RF path.</div>`;
  }

  const L = computeRFChartLayout(profile);
  const xOf = (d: number) => L.padL + (d / L.maxDist) * L.plotW;
  const yOf = (m: number) => L.padT + (1 - (m - L.minY) / (L.maxY - L.minY)) * L.plotH;

  const terrainPts = samples
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.terrain_m).toFixed(1)}`)
    .join(" ");
  const baselineY = (L.padT + L.plotH).toFixed(1);
  const terrainArea = `${L.padL.toFixed(1)},${baselineY} ${terrainPts} ${(L.padL + L.plotW).toFixed(1)},${baselineY}`;

  const losPts = samples
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.los_m).toFixed(1)}`)
    .join(" ");

  const fUpper = samples.map(
    (s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.fresnel_hi_m).toFixed(1)}`
  );
  const fLowerRev = [...samples]
    .reverse()
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.fresnel_lo_m).toFixed(1)}`);
  const fresnelPoly = [...fUpper, ...fLowerRev].join(" ");

  const f60Pts = samples
    .map((s) => `${xOf(s.dist_km).toFixed(1)},${yOf(s.fresnel60_lo_m).toFixed(1)}`)
    .join(" ");

  const accent = statusTokenColor(profile.status);
  const minSample = samples[L.minSampleIndex]!;
  const minX = xOf(minSample.dist_km);
  const minTerrainY = yOf(minSample.terrain_m);
  const minLosY = yOf(minSample.los_m);
  const minLabelOnRight = minX < L.padL + L.plotW * 0.72;
  const minBadgeX = minLabelOnRight ? minX + 8 : minX - 8;
  const minBadgeAnchor = minLabelOnRight ? "start" : "end";
  const minClearSign = minSample.clearance_m >= 0 ? "+" : "";
  const minLabelText = `${minSample.clearance_m < 0 ? "Blocked" : "Min"} ${minClearSign}${minSample.clearance_m.toFixed(1)}m`;

  // Elevation gridlines (4 levels)
  const yTicks = [0, 0.333, 0.667, 1].map((frac) => {
    const val = L.minY + (1 - frac) * (L.maxY - L.minY);
    const y = L.padT + frac * L.plotH;
    return { val: Math.round(val), y };
  });

  // Distance ticks (5 levels)
  const xTicks = [0, 0.25, 0.5, 0.75, 1].map((frac) => {
    const d = frac * L.maxDist;
    const x = L.padL + frac * L.plotW;
    return { label: d === 0 ? "0 km" : `${d.toFixed(L.maxDist < 10 ? 2 : 1)} km`, x, frac };
  });

  const first = samples[0]!;
  const last = samples[samples.length - 1]!;
  const txX = xOf(first.dist_km);
  const rxX = xOf(last.dist_km);

  return `
    <div class="gf-rf-chart-wrap" data-rf-chart>
      <div class="gf-rf-scrub-bar" data-rf-scrub-bar>
        <span class="gf-rf-scrub-item">
          <span class="gf-rf-scrub-k">Hover Profile</span>
          <span class="gf-rf-scrub-v mono" data-rf-readout>
            Min @ ${minSample.dist_km.toFixed(2)} km · Terrain ${Math.round(minSample.terrain_m)}m · LOS ${Math.round(minSample.los_m)}m · Clear ${minClearSign}${minSample.clearance_m.toFixed(1)}m
          </span>
        </span>
      </div>
      <svg class="gf-rf-svg" viewBox="0 0 ${L.width} ${L.height}" role="img" aria-label="Curved-Earth 1st Fresnel terrain profile (${escapeHtml(profile.status)})">
        <defs>
          <linearGradient id="gfTerrainFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--gf-terrain-top)" stop-opacity="0.72" />
            <stop offset="100%" stop-color="var(--gf-terrain-bot)" stop-opacity="0.92" />
          </linearGradient>
        </defs>

        <rect x="0.5" y="0.5" width="${L.width - 1}" height="${L.height - 1}" rx="8" fill="var(--gf-surface-inset)" stroke="var(--gf-border-subtle)" />

        <!-- Horizontal elevation grid -->
        ${yTicks
          .map(
            (t, idx) => `
          <line x1="${L.padL}" y1="${t.y.toFixed(1)}" x2="${L.padL + L.plotW}" y2="${t.y.toFixed(1)}" stroke="var(--gf-grid-line)" stroke-width="${idx === yTicks.length - 1 ? "1" : "0.75"}" ${idx > 0 && idx < yTicks.length - 1 ? 'stroke-dasharray="3 3"' : ""} />
          <text x="${L.padL - 7}" y="${(t.y + 3).toFixed(1)}" text-anchor="end" fill="var(--gf-text-muted)" font-size="9.5" font-family="var(--gf-font-mono)">${t.val}m</text>
        `
          )
          .join("")}

        <!-- Vertical distance ticks -->
        ${xTicks
          .map((t) => {
            const anchor = t.frac === 0 ? "start" : t.frac === 1 ? "end" : "middle";
            return `
              <line x1="${t.x.toFixed(1)}" y1="${L.padT}" x2="${t.x.toFixed(1)}" y2="${L.padT + L.plotH}" stroke="var(--gf-grid-line)" stroke-width="0.6" stroke-dasharray="2 4" opacity="0.65" />
              <text x="${t.x.toFixed(1)}" y="${L.height - 9}" text-anchor="${anchor}" fill="var(--gf-text-muted)" font-size="9.5" font-family="var(--gf-font-mono)">${escapeHtml(t.label)}</text>
            `;
          })
          .join("")}

        <!-- 1st Fresnel Zone 100% envelope -->
        <polygon points="${fresnelPoly}" fill="${accent}" fill-opacity="0.16" stroke="${accent}" stroke-opacity="0.55" stroke-width="1" />

        <!-- 60% Fresnel inner clearance line -->
        <polyline points="${f60Pts}" fill="none" stroke="var(--gf-accent-amber)" stroke-width="1.15" stroke-dasharray="4 3" opacity="0.88" />

        <!-- Terrain elevation polygon & ridge line -->
        <polygon points="${terrainArea}" fill="url(#gfTerrainFill)" />
        <polyline points="${terrainPts}" fill="none" stroke="var(--gf-terrain-stroke)" stroke-width="1.65" stroke-linejoin="round" />

        <!-- Line-of-Sight ray -->
        <polyline points="${losPts}" fill="none" stroke="${accent}" stroke-width="1.8" stroke-dasharray="6 3" />

        <!-- TX / RX Antenna Masts -->
        <line x1="${txX.toFixed(1)}" y1="${yOf(first.terrain_m).toFixed(1)}" x2="${txX.toFixed(1)}" y2="${yOf(first.los_m).toFixed(1)}" stroke="var(--gf-accent-cyan)" stroke-width="2.2" />
        <circle cx="${txX.toFixed(1)}" cy="${yOf(first.los_m).toFixed(1)}" r="3.4" fill="var(--gf-accent-cyan)" stroke="var(--gf-surface-inset)" stroke-width="1.2" />
        <text x="${(txX + 5).toFixed(1)}" y="${Math.max(L.padT + 10, yOf(first.los_m) - 6).toFixed(1)}" fill="var(--gf-text-primary)" font-size="9.5" font-weight="600" font-family="var(--gf-font-mono)">${escapeHtml(profile.from_label || "TX")} (+${profile.from_height_m}m)</text>

        <line x1="${rxX.toFixed(1)}" y1="${yOf(last.terrain_m).toFixed(1)}" x2="${rxX.toFixed(1)}" y2="${yOf(last.los_m).toFixed(1)}" stroke="var(--gf-accent-cyan)" stroke-width="2.2" />
        <circle cx="${rxX.toFixed(1)}" cy="${yOf(last.los_m).toFixed(1)}" r="3.4" fill="var(--gf-accent-cyan)" stroke="var(--gf-surface-inset)" stroke-width="1.2" />
        <text x="${(rxX - 5).toFixed(1)}" y="${Math.max(L.padT + 10, yOf(last.los_m) - 6).toFixed(1)}" text-anchor="end" fill="var(--gf-text-primary)" font-size="9.5" font-weight="600" font-family="var(--gf-font-mono)">${escapeHtml(profile.to_label || "RX")} (+${profile.to_height_m}m)</text>

        <!-- Worst Clearance Pin -->
        <line x1="${minX.toFixed(1)}" y1="${minTerrainY.toFixed(1)}" x2="${minX.toFixed(1)}" y2="${minLosY.toFixed(1)}" stroke="${accent}" stroke-width="1.4" stroke-dasharray="2 2" />
        <circle cx="${minX.toFixed(1)}" cy="${minTerrainY.toFixed(1)}" r="3.2" fill="${accent}" stroke="var(--gf-surface-inset)" stroke-width="1.2" />
        <text x="${minBadgeX.toFixed(1)}" y="${Math.max(L.padT + 12, Math.min(minTerrainY, minLosY) - 5).toFixed(1)}" text-anchor="${minBadgeAnchor}" fill="${accent}" font-size="9.5" font-weight="700" font-family="var(--gf-font-mono)">${escapeHtml(minLabelText)}</text>

        <!-- Dynamic Scrub Reticle (hidden until hover) -->
        <g data-rf-reticle style="display:none;pointer-events:none">
          <line data-rf-vline x1="0" y1="${L.padT}" x2="0" y2="${L.padT + L.plotH}" stroke="var(--gf-accent-cyan)" stroke-width="1.2" stroke-dasharray="3 2" />
          <circle data-rf-dot-los cx="0" cy="0" r="3.8" fill="${accent}" stroke="var(--gf-bg-card)" stroke-width="1.4" />
          <circle data-rf-dot-f1 cx="0" cy="0" r="3" fill="var(--gf-accent-amber)" stroke="var(--gf-bg-card)" stroke-width="1.2" />
          <circle data-rf-dot-terrain cx="0" cy="0" r="3.8" fill="var(--gf-accent-cyan)" stroke="var(--gf-bg-card)" stroke-width="1.4" />
        </g>

        <!-- Pointer Hit Area -->
        <rect data-rf-hit x="${L.padL}" y="${L.padT}" width="${L.plotW}" height="${L.plotH}" fill="transparent" style="cursor:crosshair" />
      </svg>
      <div class="gf-rf-legend">
        <span><i class="gf-leg-swatch gf-leg-los" style="background:${accent}"></i>LOS Ray</span>
        <span><i class="gf-leg-swatch gf-leg-f1" style="background:${accent}"></i>100% F1 Zone</span>
        <span><i class="gf-leg-swatch gf-leg-f60"></i>60% F1 Threshold</span>
        <span><i class="gf-leg-swatch gf-leg-terrain"></i>Curved-Earth Terrain</span>
      </div>
    </div>
  `.trim();
}

/**
 * Mounts interactive hover/touch scrubbing onto a container holding `buildRFChartMarkup`.
 */
export function mountRFChart(container: HTMLElement, profile: RFLinkProfileData): () => void {
  container.innerHTML = buildRFChartMarkup(profile);
  const samples = profile.samples ?? [];
  if (samples.length < 2) return () => {};

  const L = computeRFChartLayout(profile);
  const xOf = (d: number) => L.padL + (d / L.maxDist) * L.plotW;
  const yOf = (m: number) => L.padT + (1 - (m - L.minY) / (L.maxY - L.minY)) * L.plotH;

  const svg = container.querySelector<SVGSVGElement>(".gf-rf-svg");
  const hit = container.querySelector<SVGRectElement>("[data-rf-hit]");
  const reticle = container.querySelector<SVGGElement>("[data-rf-reticle]");
  const vline = container.querySelector<SVGLineElement>("[data-rf-vline]");
  const dotLos = container.querySelector<SVGCircleElement>("[data-rf-dot-los]");
  const dotF1 = container.querySelector<SVGCircleElement>("[data-rf-dot-f1]");
  const dotTerrain = container.querySelector<SVGCircleElement>("[data-rf-dot-terrain]");
  const readout = container.querySelector<HTMLElement>("[data-rf-readout]");

  if (!svg || !hit || !reticle || !vline || !dotLos || !dotF1 || !dotTerrain || !readout) {
    return () => {};
  }

  const defaultText = readout.textContent ?? "";

  const updateAtClientX = (clientX: number) => {
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0) return;
    const svgX = ((clientX - rect.left) / rect.width) * L.width;
    const clampedX = Math.max(L.padL, Math.min(L.padL + L.plotW, svgX));
    const distKm = ((clampedX - L.padL) / L.plotW) * L.maxDist;
    const found = findNearestRFSample(samples, distKm);
    if (!found) return;
    const s = found.sample;
    const sx = xOf(s.dist_km);
    const syLos = yOf(s.los_m);
    const syF1 = yOf(s.fresnel_lo_m);
    const syTerr = yOf(s.terrain_m);

    reticle.style.display = "block";
    vline.setAttribute("x1", sx.toFixed(1));
    vline.setAttribute("x2", sx.toFixed(1));
    dotLos.setAttribute("cx", sx.toFixed(1));
    dotLos.setAttribute("cy", syLos.toFixed(1));
    dotF1.setAttribute("cx", sx.toFixed(1));
    dotF1.setAttribute("cy", syF1.toFixed(1));
    dotTerrain.setAttribute("cx", sx.toFixed(1));
    dotTerrain.setAttribute("cy", syTerr.toFixed(1));

    const sign = s.clearance_m >= 0 ? "+" : "";
    readout.textContent = `${s.dist_km.toFixed(2)} km · Terrain ${Math.round(s.terrain_m)}m · LOS ${Math.round(s.los_m)}m · F1 Low ${Math.round(s.fresnel_lo_m)}m · Clear ${sign}${s.clearance_m.toFixed(1)}m`;
  };

  const onPointerMove = (ev: PointerEvent) => {
    updateAtClientX(ev.clientX);
  };
  const onPointerLeave = () => {
    reticle.style.display = "none";
    readout.textContent = defaultText;
  };

  hit.addEventListener("pointermove", onPointerMove);
  hit.addEventListener("pointerdown", onPointerMove);
  hit.addEventListener("pointerleave", onPointerLeave);

  return () => {
    hit.removeEventListener("pointermove", onPointerMove);
    hit.removeEventListener("pointerdown", onPointerMove);
    hit.removeEventListener("pointerleave", onPointerLeave);
  };
}
