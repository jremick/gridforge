import type { Feature, FeatureCollection, Position } from "geojson";
import { escapeHtml, type ViewshedObserverData, type WaypointItem } from "./types.ts";

export interface GeoBounds {
  minLon: number;
  maxLon: number;
  minLat: number;
  maxLat: number;
  centerLon: number;
  centerLat: number;
}

export interface CanvasProjection {
  width: number;
  height: number;
  centerLon: number;
  centerLat: number;
  cosLat: number;
  pxPerDegLat: number;
  panX: number;
  panY: number;
}

export interface MapCanvasData {
  center?: { lat: number; lon: number } | null;
  footprintGeoJson?: FeatureCollection | null;
  gridGeoJson?: FeatureCollection | null;
  viewshedGeoJson?: FeatureCollection | null;
  rangeRingGeoJson?: FeatureCollection | null;
  routeGeoJson?: FeatureCollection | null;
  waypoints?: WaypointItem[];
  connectWaypoints?: boolean;
  observer?: ViewshedObserverData | null;
  hintLabel?: string;
}

export interface MapCanvasCallbacks {
  onCenterClick?: (lat: number, lon: number) => void;
}

function collectPositions(coords: unknown, out: [number, number][]): void {
  if (!Array.isArray(coords)) return;
  if (
    coords.length >= 2 &&
    typeof coords[0] === "number" &&
    typeof coords[1] === "number"
  ) {
    if (Number.isFinite(coords[0]) && Number.isFinite(coords[1])) {
      out.push([coords[0], coords[1]]);
    }
    return;
  }
  for (const item of coords) {
    collectPositions(item, out);
  }
}

function collectFromFeatureCollection(
  fc: FeatureCollection | null | undefined,
  out: [number, number][]
): void {
  if (!fc || !Array.isArray(fc.features)) return;
  for (const f of fc.features) {
    const geom = f?.geometry;
    if (!geom) continue;
    if (geom.type === "GeometryCollection") {
      for (const g of geom.geometries) {
        if ("coordinates" in g) collectPositions(g.coordinates, out);
      }
    } else if ("coordinates" in geom) {
      collectPositions(geom.coordinates, out);
    }
  }
}

/**
 * Computes geographic bounding box across all active GeoJSON layers and points.
 */
export function computeGeoBounds(data: MapCanvasData): GeoBounds {
  const pts: [number, number][] = [];
  collectFromFeatureCollection(data.footprintGeoJson, pts);
  collectFromFeatureCollection(data.viewshedGeoJson, pts);
  collectFromFeatureCollection(data.rangeRingGeoJson, pts);
  collectFromFeatureCollection(data.routeGeoJson, pts);
  // Only fall back to grid lines if no primary polygon/route bounds exist
  if (pts.length === 0) {
    collectFromFeatureCollection(data.gridGeoJson, pts);
  }
  if (Array.isArray(data.waypoints)) {
    for (const wp of data.waypoints) {
      if (Number.isFinite(wp.lon) && Number.isFinite(wp.lat)) {
        pts.push([wp.lon, wp.lat]);
      }
    }
  }
  if (data.observer && Number.isFinite(data.observer.lon) && Number.isFinite(data.observer.lat)) {
    pts.push([data.observer.lon, data.observer.lat]);
  }
  if (data.center && Number.isFinite(data.center.lon) && Number.isFinite(data.center.lat)) {
    pts.push([data.center.lon, data.center.lat]);
  }

  if (pts.length === 0) {
    return {
      minLon: 150.28,
      maxLon: 150.34,
      minLat: -33.74,
      maxLat: -33.69,
      centerLon: 150.31,
      centerLat: -33.715,
    };
  }

  let minLon = Infinity;
  let maxLon = -Infinity;
  let minLat = Infinity;
  let maxLat = -Infinity;
  for (const [lon, lat] of pts) {
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
  }

  // Ensure non-degenerate span
  const minSpan = 0.015;
  if (maxLon - minLon < minSpan) {
    const mid = (minLon + maxLon) / 2;
    minLon = mid - minSpan / 2;
    maxLon = mid + minSpan / 2;
  }
  if (maxLat - minLat < minSpan) {
    const mid = (minLat + maxLat) / 2;
    minLat = mid - minSpan / 2;
    maxLat = mid + minSpan / 2;
  }

  return {
    minLon,
    maxLon,
    minLat,
    maxLat,
    centerLon: (minLon + maxLon) / 2,
    centerLat: (minLat + maxLat) / 2,
  };
}

export function createFitProjection(
  bounds: GeoBounds,
  width: number,
  height: number,
  padding = 28
): CanvasProjection {
  const cosLat = Math.max(0.15, Math.cos((bounds.centerLat * Math.PI) / 180));
  const spanLat = Math.max(bounds.maxLat - bounds.minLat, 0.002);
  const spanLonEq = Math.max((bounds.maxLon - bounds.minLon) * cosLat, 0.002);
  const usableW = Math.max(40, width - padding * 2);
  const usableH = Math.max(40, height - padding * 2);
  const pxPerDegLat = Math.min(usableW / spanLonEq, usableH / spanLat);

  return {
    width,
    height,
    centerLon: bounds.centerLon,
    centerLat: bounds.centerLat,
    cosLat,
    pxPerDegLat,
    panX: 0,
    panY: 0,
  };
}

export function projectLonLat(
  lon: number,
  lat: number,
  proj: CanvasProjection
): [number, number] {
  const x =
    proj.width / 2 +
    proj.panX +
    (lon - proj.centerLon) * proj.cosLat * proj.pxPerDegLat;
  const y =
    proj.height / 2 +
    proj.panY -
    (lat - proj.centerLat) * proj.pxPerDegLat;
  return [x, y];
}

export function unprojectCanvasXY(
  x: number,
  y: number,
  proj: CanvasProjection
): [number, number] {
  const lon =
    proj.centerLon +
    (x - proj.width / 2 - proj.panX) / (proj.cosLat * proj.pxPerDegLat);
  const lat =
    proj.centerLat -
    (y - proj.height / 2 - proj.panY) / proj.pxPerDegLat;
  return [lon, lat];
}

/**
 * Picks a clean rounded scale bar length in meters and pixel width.
 */
export function computeScaleBar(
  pxPerDegLat: number,
  targetPx = 92
): { label: string; widthPx: number } {
  const metersPerDegLat = 111_320;
  const metersPerPx = metersPerDegLat / Math.max(1, pxPerDegLat);
  const rawMeters = metersPerPx * targetPx;
  const steps = [50, 100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000];
  let chosen = steps[0]!;
  for (const s of steps) {
    if (s <= rawMeters * 1.25) chosen = s;
  }
  const widthPx = Math.max(28, Math.min(140, chosen / metersPerPx));
  const label = chosen >= 1000 ? `${chosen / 1000} km` : `${chosen} m`;
  return { label, widthPx };
}

interface CanvasPalette {
  bg: string;
  graticule: string;
  gridLine: string;
  gridText: string;
  footprintFill: string;
  footprintStroke: string;
  footprintText: string;
  footprintPillBg: string;
  viewshedFill: string;
  viewshedStroke: string;
  ringStroke: string;
  routeStroke: string;
  routeHalo: string;
  crosshair: string;
  hudText: string;
  scaleBarBg: string;
  scaleBarFg: string;
}

function readCanvasPalette(el: HTMLElement): CanvasPalette {
  const cs = getComputedStyle(el);
  const isLight = document.documentElement.getAttribute("data-theme") === "light";
  const get = (name: string, fallback: string) =>
    cs.getPropertyValue(name).trim() || fallback;

  return {
    bg: get("--gf-map-bg", isLight ? "#f1f5f9" : "#0b1117"),
    graticule: isLight ? "rgba(15, 23, 42, 0.06)" : "rgba(148, 163, 184, 0.08)",
    gridLine: isLight ? "rgba(2, 132, 199, 0.34)" : "rgba(56, 189, 248, 0.30)",
    gridText: isLight ? "#0369a1" : "#7dd3fc",
    footprintFill: isLight ? "rgba(217, 119, 6, 0.12)" : "rgba(245, 158, 11, 0.13)",
    footprintStroke: isLight ? "#b45309" : "#f59e0b",
    footprintText: isLight ? "#92400e" : "#fcd34d",
    footprintPillBg: isLight ? "rgba(255, 255, 255, 0.88)" : "rgba(11, 17, 23, 0.84)",
    viewshedFill: isLight ? "rgba(5, 150, 105, 0.28)" : "rgba(16, 185, 129, 0.30)",
    viewshedStroke: isLight ? "#047857" : "#34d399",
    ringStroke: isLight ? "#0284c7" : "#38bdf8",
    routeStroke: isLight ? "#0284c7" : "#38bdf8",
    routeHalo: isLight ? "rgba(255, 255, 255, 0.85)" : "rgba(11, 17, 23, 0.90)",
    crosshair: isLight ? "#dc2626" : "#fbbf24",
    hudText: isLight ? "#334155" : "#cbd5e1",
    scaleBarBg: isLight ? "rgba(255, 255, 255, 0.88)" : "rgba(15, 20, 25, 0.82)",
    scaleBarFg: isLight ? "#0f172a" : "#e2e8f0",
  };
}

function waypointColor(category: string | undefined): string {
  switch ((category || "").toLowerCase()) {
    case "camp":
      return "#10b981";
    case "water":
      return "#38bdf8";
    case "hazard":
      return "#ef4444";
    case "lz":
      return "#f59e0b";
    default:
      return "#a78bfa";
  }
}

function traceRing(
  ctx: CanvasRenderingContext2D,
  ring: Position[],
  proj: CanvasProjection
): void {
  if (ring.length === 0) return;
  const [x0, y0] = projectLonLat(ring[0]![0]!, ring[0]![1]!, proj);
  ctx.moveTo(x0, y0);
  for (let i = 1; i < ring.length; i++) {
    const [x, y] = projectLonLat(ring[i]![0]!, ring[i]![1]!, proj);
    ctx.lineTo(x, y);
  }
  ctx.closePath();
}

function computePolygonCentroid(ring: Position[], proj: CanvasProjection): [number, number] | null {
  if (ring.length < 3) return null;
  let sx = 0;
  let sy = 0;
  const n = ring.length > 1 &&
    ring[0]![0] === ring[ring.length - 1]![0] &&
    ring[0]![1] === ring[ring.length - 1]![1]
    ? ring.length - 1
    : ring.length;
  if (n === 0) return null;
  for (let i = 0; i < n; i++) {
    const [x, y] = projectLonLat(ring[i]![0]!, ring[i]![1]!, proj);
    sx += x;
    sy += y;
  }
  return [sx / n, sy / n];
}

function drawPolygons(
  ctx: CanvasRenderingContext2D,
  fc: FeatureCollection,
  proj: CanvasProjection,
  fill: string,
  stroke: string,
  lineWidth: number,
  options: { labelProp?: string[]; pillBg?: string; textColor?: string; dashed?: boolean } = {}
): void {
  const labels: Array<{ text: string; x: number; y: number }> = [];
  ctx.save();
  ctx.fillStyle = fill;
  ctx.strokeStyle = stroke;
  ctx.lineWidth = lineWidth;
  if (options.dashed) ctx.setLineDash([5, 4]);

  for (const f of fc.features) {
    const g = f?.geometry;
    if (!g) continue;
    let firstOuterRing: Position[] | null = null;
    if (g.type === "Polygon") {
      ctx.beginPath();
      for (const ring of g.coordinates) {
        if (!firstOuterRing) firstOuterRing = ring;
        traceRing(ctx, ring, proj);
      }
      ctx.fill("evenodd");
      ctx.stroke();
    } else if (g.type === "MultiPolygon") {
      for (const poly of g.coordinates) {
        ctx.beginPath();
        for (const ring of poly) {
          if (!firstOuterRing) firstOuterRing = ring;
          traceRing(ctx, ring, proj);
        }
        ctx.fill("evenodd");
        ctx.stroke();
      }
    }

    if (options.labelProp && firstOuterRing && f.properties) {
      let labelText = "";
      for (const key of options.labelProp) {
        const val = (f.properties as Record<string, unknown>)[key];
        if (val != null && String(val).trim()) {
          labelText = String(val).trim();
          break;
        }
      }
      if (labelText) {
        const c = computePolygonCentroid(firstOuterRing, proj);
        if (c) labels.push({ text: labelText, x: c[0], y: c[1] });
      }
    }
  }
  ctx.restore();

  if (labels.length > 0 && options.textColor && options.pillBg) {
    ctx.save();
    ctx.font = '700 11px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const lbl of labels) {
      const tw = ctx.measureText(lbl.text).width;
      const pw = tw + 12;
      const ph = 18;
      ctx.fillStyle = options.pillBg;
      ctx.beginPath();
      ctx.roundRect(lbl.x - pw / 2, lbl.y - ph / 2, pw, ph, 4);
      ctx.fill();
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = options.textColor;
      ctx.fillText(lbl.text, lbl.x, lbl.y + 0.5);
    }
    ctx.restore();
  }
}

function drawLines(
  ctx: CanvasRenderingContext2D,
  fc: FeatureCollection,
  proj: CanvasProjection,
  stroke: string,
  lineWidth: number,
  options: { dashed?: number[]; labelColor?: string } = {}
): void {
  ctx.save();
  ctx.strokeStyle = stroke;
  ctx.lineWidth = lineWidth;
  if (options.dashed) ctx.setLineDash(options.dashed);

  const labelHints: Array<{ text: string; x: number; y: number; axis: string }> = [];

  const drawSingleLine = (coords: Position[], feature: Feature) => {
    if (coords.length < 2) return;
    ctx.beginPath();
    const [x0, y0] = projectLonLat(coords[0]![0]!, coords[0]![1]!, proj);
    ctx.moveTo(x0, y0);
    for (let i = 1; i < coords.length; i++) {
      const [x, y] = projectLonLat(coords[i]![0]!, coords[i]![1]!, proj);
      ctx.lineTo(x, y);
    }
    ctx.stroke();

    if (options.labelColor && feature.properties) {
      const p = feature.properties as Record<string, unknown>;
      const lbl = p.label ?? p.principal_digits ?? p.digits ?? p.value;
      const axis = String(p.axis ?? p.orientation ?? "");
      if (lbl != null && String(lbl).trim()) {
        labelHints.push({ text: String(lbl).trim(), x: x0, y: y0, axis });
      }
    }
  };

  for (const f of fc.features) {
    const g = f?.geometry;
    if (!g) continue;
    if (g.type === "LineString") {
      drawSingleLine(g.coordinates, f);
    } else if (g.type === "MultiLineString") {
      for (const line of g.coordinates) {
        drawSingleLine(line, f);
      }
    }
  }
  ctx.restore();

  if (options.labelColor && labelHints.length > 0) {
    ctx.save();
    ctx.font = '600 9px "JetBrains Mono", ui-monospace, monospace';
    ctx.fillStyle = options.labelColor;
    ctx.textBaseline = "middle";
    // Throttle overlapping grid labels
    const placed: Array<[number, number]> = [];
    for (const h of labelHints) {
      if (h.x < 6 || h.x > proj.width - 20 || h.y < 10 || h.y > proj.height - 20) {
        continue;
      }
      if (placed.some(([px, py]) => Math.hypot(px - h.x, py - h.y) < 26)) continue;
      placed.push([h.x, h.y]);
      ctx.fillText(h.text, h.x + 3, h.y - 5);
    }
    ctx.restore();
  }
}

export interface MountedMapCanvas {
  redraw: () => void;
  fit: () => void;
  destroy: () => void;
}

/**
 * Mounts an interactive pan/zoom/click HTML5 Canvas vector map inside `container`.
 */
export function mountMapCanvas(
  container: HTMLElement,
  data: MapCanvasData,
  callbacks: MapCanvasCallbacks = {}
): MountedMapCanvas {
  const interactiveHint = callbacks.onCenterClick
    ? data.hintLabel || "Drag to pan · Scroll to zoom · Click map to recenter"
    : data.hintLabel || "Drag to pan · Scroll to zoom";

  container.innerHTML = `
    <div class="gf-map-stage" data-map-stage>
      <canvas class="gf-map-canvas" data-map-canvas aria-label="Interactive GridForge vector map preview"></canvas>
      <div class="gf-map-controls" role="toolbar" aria-label="Map zoom controls">
        <button type="button" class="gf-map-btn" data-map-zoom="in" title="Zoom in" aria-label="Zoom in">+</button>
        <button type="button" class="gf-map-btn" data-map-zoom="out" title="Zoom out" aria-label="Zoom out">−</button>
        <button type="button" class="gf-map-btn gf-map-btn-fit" data-map-zoom="fit" title="Fit extent" aria-label="Fit map extent">Fit</button>
      </div>
      <div class="gf-map-hud-bl mono" data-map-coords>${
        data.center
          ? `${data.center.lat.toFixed(5)}, ${data.center.lon.toFixed(5)}`
          : "Hover map for coordinates"
      }</div>
      <div class="gf-map-hud-hint">${escapeHtml(interactiveHint)}</div>
    </div>
  `.trim();

  const stage = container.querySelector<HTMLElement>("[data-map-stage]")!;
  const canvas = container.querySelector<HTMLCanvasElement>("[data-map-canvas]")!;
  const coordsEl = container.querySelector<HTMLElement>("[data-map-coords]")!;
  const ctx = canvas.getContext("2d");

  const bounds = computeGeoBounds(data);
  let proj: CanvasProjection = createFitProjection(bounds, 560, 280);
  let basePxPerDeg = proj.pxPerDegLat;
  let clickedTarget: { lat: number; lon: number } | null = null;
  let hoverCoord: { lat: number; lon: number } | null = null;

  const resizeToContainer = (resetView = false) => {
    const rect = stage.getBoundingClientRect();
    const cssW = Math.max(220, Math.round(rect.width || 560));
    const cssH = Math.max(180, Math.round(rect.height || 270));
    const dpr = Math.min(2.5, window.devicePixelRatio || 1);

    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      canvas.style.width = `${cssW}px`;
      canvas.style.height = `${cssH}px`;
    }

    if (resetView) {
      proj = createFitProjection(bounds, cssW, cssH, 30);
      basePxPerDeg = proj.pxPerDegLat;
    } else {
      proj.width = cssW;
      proj.height = cssH;
    }
    redraw();
  };

  const redraw = () => {
    if (!ctx) return;
    const dpr = canvas.width / Math.max(1, proj.width);
    const pal = readCanvasPalette(stage);

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // 1. Background + subtle coordinate backdrop grid
    ctx.fillStyle = pal.bg;
    ctx.fillRect(0, 0, proj.width, proj.height);

    ctx.strokeStyle = pal.graticule;
    ctx.lineWidth = 1;
    const stepPx = 42;
    ctx.beginPath();
    for (let x = ((proj.width / 2 + proj.panX) % stepPx + stepPx) % stepPx; x < proj.width; x += stepPx) {
      ctx.moveTo(x, 0);
      ctx.lineTo(x, proj.height);
    }
    for (let y = ((proj.height / 2 + proj.panY) % stepPx + stepPx) % stepPx; y < proj.height; y += stepPx) {
      ctx.moveTo(0, y);
      ctx.lineTo(proj.width, y);
    }
    ctx.stroke();

    // 2. Viewshed coverage polygons
    if (data.viewshedGeoJson) {
      drawPolygons(
        ctx,
        data.viewshedGeoJson,
        proj,
        pal.viewshedFill,
        pal.viewshedStroke,
        1.25
      );
    }

    // 3. Range ring
    if (data.rangeRingGeoJson) {
      drawPolygons(
        ctx,
        data.rangeRingGeoJson,
        proj,
        "transparent",
        pal.ringStroke,
        1.3,
        { dashed: true }
      );
      drawLines(ctx, data.rangeRingGeoJson, proj, pal.ringStroke, 1.3, {
        dashed: [5, 4],
      });
    }

    // 4. 1 km MGRS Grid lines
    if (data.gridGeoJson) {
      drawLines(ctx, data.gridGeoJson, proj, pal.gridLine, 0.9, {
        labelColor: pal.gridText,
      });
    }

    // 5. True-scale Sheet Footprints (A1..C3 or S1..Sn)
    if (data.footprintGeoJson) {
      drawPolygons(
        ctx,
        data.footprintGeoJson,
        proj,
        pal.footprintFill,
        pal.footprintStroke,
        1.9,
        {
          labelProp: ["sheet_id", "cell_id", "label", "name", "id"],
          pillBg: pal.footprintPillBg,
          textColor: pal.footprintText,
        }
      );
    }

    // 6. Route GeoJSON or connected waypoints
    if (data.routeGeoJson) {
      drawLines(ctx, data.routeGeoJson, proj, pal.routeHalo, 5);
      drawLines(ctx, data.routeGeoJson, proj, pal.routeStroke, 2.5);
    } else if (data.connectWaypoints && data.waypoints && data.waypoints.length >= 2) {
      ctx.save();
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      const pts = data.waypoints.map((w) => projectLonLat(w.lon, w.lat, proj));
      ctx.beginPath();
      ctx.moveTo(pts[0]![0], pts[0]![1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i]![0], pts[i]![1]);
      ctx.strokeStyle = pal.routeHalo;
      ctx.lineWidth = 5;
      ctx.stroke();
      ctx.strokeStyle = pal.routeStroke;
      ctx.lineWidth = 2.4;
      ctx.stroke();
      ctx.restore();
    }

    // 7. Waypoints
    if (data.waypoints && data.waypoints.length > 0) {
      ctx.save();
      ctx.font = '600 10px "JetBrains Mono", ui-monospace, monospace';
      ctx.textBaseline = "middle";
      data.waypoints.forEach((wp, idx) => {
        const [x, y] = projectLonLat(wp.lon, wp.lat, proj);
        const col = waypointColor(wp.category);
        ctx.beginPath();
        ctx.arc(x, y, 5.5, 0, Math.PI * 2);
        ctx.fillStyle = col;
        ctx.fill();
        ctx.strokeStyle = pal.routeHalo;
        ctx.lineWidth = 1.8;
        ctx.stroke();

        const tag = wp.code || wp.label || `W${idx + 1}`;
        const label = wp.code && wp.label && wp.label !== wp.code ? `${wp.code} ${wp.label}` : tag;
        const tw = ctx.measureText(label).width;
        const lx = x + 9;
        const ly = y;
        ctx.fillStyle = pal.footprintPillBg;
        ctx.beginPath();
        ctx.roundRect(lx - 4, ly - 8, tw + 8, 16, 4);
        ctx.fill();
        ctx.fillStyle = pal.scaleBarFg;
        ctx.fillText(label, lx, ly + 0.5);
      });
      ctx.restore();
    }

    // 8. Observer Beacon (viewshed)
    if (data.observer) {
      const [ox, oy] = projectLonLat(data.observer.lon, data.observer.lat, proj);
      ctx.save();
      ctx.beginPath();
      ctx.arc(ox, oy, 11, 0, Math.PI * 2);
      ctx.strokeStyle = pal.viewshedStroke;
      ctx.lineWidth = 1.4;
      ctx.globalAlpha = 0.6;
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.beginPath();
      ctx.arc(ox, oy, 5, 0, Math.PI * 2);
      ctx.fillStyle = pal.footprintStroke;
      ctx.fill();
      ctx.strokeStyle = pal.routeHalo;
      ctx.lineWidth = 1.6;
      ctx.stroke();

      ctx.font = '700 10px "JetBrains Mono", ui-monospace, monospace';
      const lbl = data.observer.label || "OBS";
      const tw = ctx.measureText(lbl).width;
      ctx.fillStyle = pal.footprintPillBg;
      ctx.beginPath();
      ctx.roundRect(ox + 10, oy - 9, tw + 10, 18, 4);
      ctx.fill();
      ctx.fillStyle = pal.footprintText;
      ctx.fillText(lbl, ox + 15, oy + 3);
      ctx.restore();
    }

    // 9. Center Reticle / Clicked Target Reticle
    const focusPt = clickedTarget || data.center;
    if (focusPt && !data.observer) {
      const [cx, cy] = projectLonLat(focusPt.lon, focusPt.lat, proj);
      ctx.save();
      ctx.strokeStyle = clickedTarget ? pal.crosshair : pal.footprintStroke;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.arc(cx, cy, 7, 0, Math.PI * 2);
      ctx.moveTo(cx - 12, cy);
      ctx.lineTo(cx - 4, cy);
      ctx.moveTo(cx + 4, cy);
      ctx.lineTo(cx + 12, cy);
      ctx.moveTo(cx, cy - 12);
      ctx.lineTo(cx, cy - 4);
      ctx.moveTo(cx, cy + 4);
      ctx.lineTo(cx, cy + 12);
      ctx.stroke();
      ctx.restore();
    }

    // 10. Dynamic True-Distance Scale Bar (bottom-right)
    const sb = computeScaleBar(proj.pxPerDegLat);
    const sbPadX = 10;
    const sbPadY = 10;
    const sbBoxW = sb.widthPx + 18;
    const sbBoxH = 22;
    const sbX = proj.width - sbBoxW - sbPadX;
    const sbY = proj.height - sbBoxH - sbPadY;
    ctx.save();
    ctx.fillStyle = pal.scaleBarBg;
    ctx.beginPath();
    ctx.roundRect(sbX, sbY, sbBoxW, sbBoxH, 5);
    ctx.fill();
    ctx.strokeStyle = pal.scaleBarFg;
    ctx.lineWidth = 1.4;
    const barX = sbX + 9;
    const barY = sbY + 15;
    ctx.beginPath();
    ctx.moveTo(barX, barY - 4);
    ctx.lineTo(barX, barY);
    ctx.lineTo(barX + sb.widthPx, barY);
    ctx.lineTo(barX + sb.widthPx, barY - 4);
    ctx.stroke();
    ctx.font = '600 9px "JetBrains Mono", ui-monospace, monospace';
    ctx.fillStyle = pal.scaleBarFg;
    ctx.textAlign = "center";
    ctx.fillText(sb.label, barX + sb.widthPx / 2, sbY + 9);
    ctx.restore();

    // 11. Minimal North Arrow (top-left)
    ctx.save();
    ctx.fillStyle = pal.scaleBarBg;
    ctx.beginPath();
    ctx.roundRect(10, 10, 24, 28, 5);
    ctx.fill();
    ctx.fillStyle = pal.footprintStroke;
    ctx.beginPath();
    ctx.moveTo(22, 14);
    ctx.lineTo(26.5, 25);
    ctx.lineTo(22, 22.5);
    ctx.lineTo(17.5, 25);
    ctx.closePath();
    ctx.fill();
    ctx.font = '700 8px "JetBrains Mono", ui-monospace, monospace';
    ctx.fillStyle = pal.scaleBarFg;
    ctx.textAlign = "center";
    ctx.fillText("N", 22, 34);
    ctx.restore();

    ctx.restore();
  };

  // Pan / Zoom / Click interaction state
  let dragging = false;
  let dragMoved = false;
  let startX = 0;
  let startY = 0;
  let startPanX = 0;
  let startPanY = 0;

  const zoomAt = (factor: number, anchorX = proj.width / 2, anchorY = proj.height / 2) => {
    const nextScale = Math.max(
      basePxPerDeg * 0.35,
      Math.min(basePxPerDeg * 14, proj.pxPerDegLat * factor)
    );
    const ratio = nextScale / proj.pxPerDegLat;
    proj.panX = anchorX - proj.width / 2 - (anchorX - proj.width / 2 - proj.panX) * ratio;
    proj.panY = anchorY - proj.height / 2 - (anchorY - proj.height / 2 - proj.panY) * ratio;
    proj.pxPerDegLat = nextScale;
    redraw();
  };

  const onPointerDown = (e: PointerEvent) => {
    if (e.button !== 0) return;
    dragging = true;
    dragMoved = false;
    startX = e.clientX;
    startY = e.clientY;
    startPanX = proj.panX;
    startPanY = proj.panY;
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = "grabbing";
  };

  const onPointerMove = (e: PointerEvent) => {
    const rect = canvas.getBoundingClientRect();
    const lx = e.clientX - rect.left;
    const ly = e.clientY - rect.top;
    const [lon, lat] = unprojectCanvasXY(lx, ly, proj);
    hoverCoord = { lat, lon };
    coordsEl.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;

    if (!dragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.hypot(dx, dy) > 5) dragMoved = true;
    if (dragMoved) {
      proj.panX = startPanX + dx;
      proj.panY = startPanY + dy;
      redraw();
    }
  };

  const onPointerUp = (e: PointerEvent) => {
    if (!dragging) return;
    dragging = false;
    canvas.style.cursor = callbacks.onCenterClick ? "crosshair" : "grab";
    try {
      if (canvas.hasPointerCapture?.(e.pointerId)) {
        canvas.releasePointerCapture(e.pointerId);
      }
    } catch (err) {
      console.debug("Pointer capture already released:", err);
    }
    if (!dragMoved && callbacks.onCenterClick) {
      const rect = canvas.getBoundingClientRect();
      const [lon, lat] = unprojectCanvasXY(e.clientX - rect.left, e.clientY - rect.top, proj);
      clickedTarget = { lat, lon };
      redraw();
      callbacks.onCenterClick(lat, lon);
    }
  };

  const onPointerLeave = () => {
    if (!hoverCoord) return;
    if (data.center) {
      coordsEl.textContent = `${data.center.lat.toFixed(5)}, ${data.center.lon.toFixed(5)}`;
    }
  };

  const onWheel = (e: WheelEvent) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
  };

  const onControlClick = (e: Event) => {
    const btn = (e.target as HTMLElement | null)?.closest<HTMLButtonElement>("[data-map-zoom]");
    if (!btn) return;
    const action = btn.dataset.mapZoom;
    if (action === "in") zoomAt(1.32);
    else if (action === "out") zoomAt(1 / 1.32);
    else if (action === "fit") resizeToContainer(true);
  };

  canvas.style.cursor = callbacks.onCenterClick ? "crosshair" : "grab";
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("pointerleave", onPointerLeave);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  stage.addEventListener("click", onControlClick);

  let ro: ResizeObserver | null = null;
  if (typeof ResizeObserver !== "undefined") {
    ro = new ResizeObserver(() => resizeToContainer(false));
    ro.observe(stage);
  }

  let mo: MutationObserver | null = null;
  if (typeof MutationObserver !== "undefined") {
    mo = new MutationObserver(() => redraw());
    mo.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "style"],
    });
  }

  resizeToContainer(true);

  return {
    redraw,
    fit: () => resizeToContainer(true),
    destroy: () => {
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      canvas.removeEventListener("wheel", onWheel);
      stage.removeEventListener("click", onControlClick);
      ro?.disconnect();
      mo?.disconnect();
    },
  };
}
