import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildModelContextSummary,
  escapeHtml,
  extractStructuredPayload,
  formatArtifactLabel,
  formatBytes,
  formatScale,
  formatSignedDeg,
  formatSignedMils,
  isKnownViewPayload,
  isTerminalJobStatus,
  mapPackGroupToFilter,
} from "../mcp-app/types.ts";
import {
  computeCompassAngles,
  renderDeclinationCompassSvg,
} from "../mcp-app/compass.ts";
import {
  buildRFChartMarkup,
  computeRFChartLayout,
  findNearestRFSample,
} from "../mcp-app/rfChart.ts";
import {
  computeGeoBounds,
  computeScaleBar,
  createFitProjection,
  projectLonLat,
  unprojectCanvasXY,
} from "../mcp-app/mapCanvas.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BUILT_MCP_APP_PATH = path.resolve(
  __dirname,
  "../../src/gridforge/mcp/assets/mcp_app.html"
);

test("MCP App formatting and payload extraction helpers handle all view contracts safely", () => {
  assert.equal(escapeHtml(`<img src="x" onerror='alert(1)'>`), "&lt;img src=&quot;x&quot; onerror=&#39;alert(1)&#39;&gt;");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(2048), "2.0 KB");
  assert.equal(formatBytes(4_500_000), "4.3 MB");
  assert.equal(formatSignedDeg(12.38), "+12.4° E");
  assert.equal(formatSignedDeg(-6.5), "−6.5° W");
  assert.equal(formatSignedMils(220), "+220 mils");
  assert.equal(formatScale(25000), "1:25,000");
  assert.equal(formatArtifactLabel("mbtiles"), "ATAK MBTiles");
  assert.equal(formatArtifactLabel("kmz"), "Garmin KMZ");
  assert.equal(formatArtifactLabel("zip"), "Field Bundle ZIP");

  assert.equal(mapPackGroupToFilter("au_tactical"), "AU");
  assert.equal(mapPackGroupToFilter("au_state"), "AU");
  assert.equal(mapPackGroupToFilter("nz"), "NZ");
  assert.equal(mapPackGroupToFilter("na"), "NA");

  assert.equal(isTerminalJobStatus("queued"), false);
  assert.equal(isTerminalJobStatus("running"), false);
  assert.equal(isTerminalJobStatus("succeeded"), true);
  assert.equal(isTerminalJobStatus("failed"), true);
  assert.equal(isTerminalJobStatus("cancelled"), true);

  assert.equal(isKnownViewPayload({ view: "briefing" }), true);
  assert.equal(isKnownViewPayload({ view: "rf_link" }), true);
  assert.equal(isKnownViewPayload({ view: "unknown" }), false);

  const extracted = extractStructuredPayload({
    structuredContent: { view: "studio", title: "GridForge Studio" },
    content: [{ type: "text", text: "Studio open" }],
  });
  assert.equal(extracted?.view, "studio");

  const extractedJsonFallback = extractStructuredPayload({
    content: [{ type: "text", text: JSON.stringify({ view: "geocode", results: [] }) }],
  });
  assert.equal(extractedJsonFallback?.view, "geocode");

  const ctxSummary = buildModelContextSummary({
    view: "briefing",
    location: {
      center: "-33.7152, 150.3119",
      lat: -33.7152,
      lon: 150.3119,
      mgrs_10m: "56H LH 4352 6945",
      utm_epsg: 32756,
      utm_zone: 56,
      utm_hemisphere: "S",
      gzd: "56H",
      square_ids_100km: ["LH"],
      place_name: "Katoomba",
    },
    sheet: {
      scale: 25000,
      paper: "a4",
      orientation: "portrait",
      color_mode: "color",
      units: "metric",
      page_width_mm: 210,
      page_height_mm: 297,
      ground_width_km: 4.65,
      ground_height_km: 6.55,
      grid_cols: 1,
      grid_rows: 1,
      sheet_count: 1,
      grid_spacing_mm: 40,
    },
    declination: {
      model_name: "WMM2025",
      decimal_year: 2026.9,
      magnetic_declination_deg: 12.8,
      annual_change_deg: 0.02,
      grid_convergence_deg: 0.42,
      gm_angle_deg: 12.38,
      gm_angle_mils: 220,
    },
  });
  assert.match(ctxSummary, /56H LH 4352 6945/);
  assert.match(ctxSummary, /EPSG:32756/);
  assert.match(ctxSummary, /\+220 mils/);
});

test("WMM2025 declination compass renders crisp themed SVG with distinct TN/GN/MN rays", () => {
  const dec = {
    model_name: "WMM2025",
    decimal_year: 2026.9,
    magnetic_declination_deg: 12.8,
    annual_change_deg: 0.02,
    grid_convergence_deg: 0.42,
    gm_angle_deg: 12.38,
    gm_angle_mils: 220,
  };
  const angles = computeCompassAngles(dec);
  assert.equal(angles.trueDeg, 0);
  assert.ok(angles.magDeg > angles.gridDeg, "MN should sit east of GN for positive G-M");
  assert.ok(Math.abs(angles.magDeg - angles.gridDeg) >= 6, "GN and MN rays maintain visual separation");

  const svg = renderDeclinationCompassSvg(dec, { compact: true });
  assert.match(svg, /<svg\b[^>]*class="gf-compass-svg"/);
  assert.match(svg, />★<\/text>/);
  assert.match(svg, />GN<\/text>/);
  assert.match(svg, />MN<\/text>/);
  assert.match(svg, /G-M \+12\.4° E/);
  assert.match(svg, /var\(--gf-accent-amber\)/);
});

test("RF Fresnel chart computes layout, nearest sample scrubbing, and escapes XSS labels", () => {
  const profile = {
    status: "MARGINAL",
    distance_km: 10.0,
    freq_mhz: 915,
    fspl_db: 111.6,
    min_clearance_m: 12.5,
    min_fresnel_pct: 48,
    los_clear: true,
    fresnel_60pct_clear: false,
    bearing_true_deg: 90,
    bearing_grid_deg: 89.5,
    bearing_mag_deg: 77.2,
    bearing_mils: 1372,
    from_elev_m: 800,
    to_elev_m: 850,
    from_mgrs: "56H LH 4000 6000",
    to_mgrs: "56H LH 5000 6000",
    from_label: "<TX>",
    to_label: "RX&1",
    from_height_m: 3,
    to_height_m: 4,
    samples: [
      { dist_km: 0, terrain_m: 800, los_m: 803, fresnel_lo_m: 803, fresnel_hi_m: 803, fresnel60_lo_m: 803, clearance_m: 3 },
      { dist_km: 5, terrain_m: 814, los_m: 826.5, fresnel_lo_m: 798, fresnel_hi_m: 855, fresnel60_lo_m: 809.4, clearance_m: 12.5 },
      { dist_km: 10, terrain_m: 850, los_m: 854, fresnel_lo_m: 854, fresnel_hi_m: 854, fresnel60_lo_m: 854, clearance_m: 4 },
    ],
  };

  const layout = computeRFChartLayout(profile);
  assert.equal(layout.minSampleIndex, 1);
  assert.ok(layout.maxY > 855);

  const nearest = findNearestRFSample(profile.samples, 4.7);
  assert.equal(nearest?.index, 1);
  assert.equal(nearest?.sample.dist_km, 5);

  const html = buildRFChartMarkup(profile);
  assert.match(html, /class="gf-rf-svg"/);
  assert.match(html, /&lt;TX&gt;/);
  assert.match(html, /RX&amp;1/);
  assert.doesNotMatch(html, /<TX>/);
  assert.match(html, /data-rf-hit/);

  const emptyHtml = buildRFChartMarkup({ ...profile, samples: [] });
  assert.match(emptyHtml, /No elevation samples/);
});

test("Map canvas projection math round-trips coordinates accurately and computes clean scale bar", () => {
  const bounds = computeGeoBounds({
    center: { lat: -33.7152, lon: 150.3119 },
    footprintGeoJson: {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: { sheet_id: "A1" },
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [150.28, -33.75],
                [150.34, -33.75],
                [150.34, -33.68],
                [150.28, -33.68],
                [150.28, -33.75],
              ],
            ],
          },
        },
      ],
    },
  });
  assert.equal(bounds.minLon, 150.28);
  assert.equal(bounds.maxLat, -33.68);

  const proj = createFitProjection(bounds, 600, 320);
  const [px, py] = projectLonLat(150.3119, -33.7152, proj);
  const [lonOut, latOut] = unprojectCanvasXY(px, py, proj);
  assert.ok(Math.abs(lonOut - 150.3119) < 1e-7);
  assert.ok(Math.abs(latOut - -33.7152) < 1e-7);

  const bar = computeScaleBar(proj.pxPerDegLat);
  assert.ok(bar.widthPx >= 28 && bar.widthPx <= 140);
  assert.match(bar.label, /^\d+\s+(m|km)$/);
});

test("Built single-file mcp_app.html exists, is < 500 KB, CSP-safe, and uses @modelcontextprotocol/ext-apps", () => {
  assert.ok(
    fs.existsSync(BUILT_MCP_APP_PATH),
    `Expected compiled MCP App at ${BUILT_MCP_APP_PATH} — run npm --prefix web run build:mcp-app`
  );
  const stat = fs.statSync(BUILT_MCP_APP_PATH);
  assert.ok(stat.size > 10_000, `Expected non-trivial singlefile bundle, got ${stat.size} bytes`);
  assert.ok(
    stat.size < 500 * 1024,
    `Expected mcp_app.html < 500 KB, got ${(stat.size / 1024).toFixed(1)} KB`
  );

  const html = fs.readFileSync(BUILT_MCP_APP_PATH, "utf8");
  assert.match(html, /<!DOCTYPE html>/i);
  assert.match(html, /<style\b/i, "CSS must be inlined");
  assert.match(html, /<script\b/i, "JS must be inlined");

  // Strict CSP: zero external scripts or stylesheets
  assert.doesNotMatch(html, /<script\b[^>]*\bsrc=/i, "Must not load external scripts");
  assert.doesNotMatch(html, /<link\b[^>]*\brel=["']stylesheet["']/i, "Must not load external stylesheets");

  // Opaque iframe origin hygiene: never rely on localStorage/IndexedDB/cookies
  assert.doesNotMatch(html, /\blocalStorage\b/, "Must not access localStorage");
  assert.doesNotMatch(html, /\bindexedDB\b/, "Must not access indexedDB");
  assert.doesNotMatch(html, /\bdocument\.cookie\b/, "Must not access document.cookie");

  // Repo privacy hygiene: zero local workstation paths
  assert.doesNotMatch(html, /\/Users\/[A-Za-z0-9._-]+\//, "Must not contain macOS home paths");
  assert.doesNotMatch(html, /\/home\/[A-Za-z0-9._-]+\//, "Must not contain Linux home paths");

  // Official MCP Apps SEP-1865 protocol wire methods bundled from @modelcontextprotocol/ext-apps
  assert.match(html, /ui\/initialize/);
  assert.match(html, /ui\/notifications\/initialized/);
  assert.match(html, /ui\/notifications\/host-context-changed/);
  assert.match(html, /ui\/notifications\/tool-result/);
});
