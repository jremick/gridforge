import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type {
  Feature,
  FeatureCollection,
  LineString,
  Point,
  Polygon,
  Position,
} from "geojson";
import {
  DEFAULT_CENTER_LAT,
  DEFAULT_CENTER_LON,
  DEFAULT_MAP_ZOOM,
  WAYPOINT_CATEGORY_OPTIONS,
} from "./presets";
import type {
  BasemapId,
  BasemapInfoResponse,
  MapInteractionMode,
  RFLinkEntry,
  TacticalGraphic,
  ViewshedEntry,
  WaypointCategory,
  WaypointInput,
} from "./types";

const EMPTY_FC: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

const CATEGORY_COLOR_MAP: Record<WaypointCategory, string> = Object.fromEntries(
  WAYPOINT_CATEGORY_OPTIONS.map((o) => [o.value, o.color])
) as Record<WaypointCategory, string>;

const EXTERNAL_SOURCE_IDS = new Set([
  "osm-standard",
  "opentopomap",
  "esri-imagery",
]);

export interface GridForgeMapCallbacks {
  onCenterDrag: (lat: number, lon: number) => void;
  onCenterCommit: (lat: number, lon: number) => void;
  onMapMarkupClick?: (lat: number, lon: number) => void;
  onMapViewshedClick?: (lat: number, lon: number) => void;
  onMapRFLinkClick?: (lat: number, lon: number) => void;
  onMapRulerClick?: (lat: number, lon: number) => void;
  onMapRingClick?: (lat: number, lon: number) => void;
  onMapSectorClick?: (lat: number, lon: number) => void;
  onMapSectorFinish?: () => void;
  onCursorMove?: (lat: number, lon: number) => void;
  onExternalTileFailure?: () => void;
}

export interface GridForgeMapOptions {
  initialLat?: number;
  initialLon?: number;
  initialZoom?: number;
}

function buildInitialStyle(): maplibregl.StyleSpecification {
  return {
    version: 8,
    sources: {
      "osm-standard": {
        type: "raster",
        tiles: [
          "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
          "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png",
          "https://c.tile.openstreetmap.org/{z}/{x}/{y}.png",
        ],
        tileSize: 256,
        maxzoom: 19,
        attribution: "© OpenStreetMap contributors",
      },
      opentopomap: {
        type: "raster",
        tiles: ["https://tile.opentopomap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        maxzoom: 17,
        attribution:
          "Map data: © OpenStreetMap contributors, SRTM | Style: © OpenTopoMap (CC-BY-SA)",
      },
      "esri-imagery": {
        type: "raster",
        tiles: [
          "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        ],
        tileSize: 256,
        maxzoom: 19,
        attribution: "Tiles © Esri — World Imagery",
      },
      "sheet-footprint": { type: "geojson", data: EMPTY_FC },
      "mgrs-grid": { type: "geojson", data: EMPTY_FC },
      "viewshed-coverage": { type: "geojson", data: EMPTY_FC },
      "viewshed-rings": { type: "geojson", data: EMPTY_FC },
      "tactical-sectors": { type: "geojson", data: EMPTY_FC },
      "tactical-rings": { type: "geojson", data: EMPTY_FC },
      "tactical-lines": { type: "geojson", data: EMPTY_FC },
      "tactical-pins": { type: "geojson", data: EMPTY_FC },
      "rf-links-lines": { type: "geojson", data: EMPTY_FC },
      "rf-links-pins": { type: "geojson", data: EMPTY_FC },
      "ruler-line": { type: "geojson", data: EMPTY_FC },
      "ruler-points": { type: "geojson", data: EMPTY_FC },
      "strip-route": { type: "geojson", data: EMPTY_FC },
      "sheet-labels": { type: "geojson", data: EMPTY_FC },
      "waypoints-legs": { type: "geojson", data: EMPTY_FC },
      "waypoints-points": { type: "geojson", data: EMPTY_FC },
      "viewshed-observers": { type: "geojson", data: EMPTY_FC },
    },
    layers: [
      {
        id: "bg-dark",
        type: "background",
        paint: { "background-color": "#0F1419" },
      },
      {
        id: "basemap-osm-underlay",
        type: "raster",
        source: "osm-standard",
        layout: { visibility: "visible" },
        paint: { "raster-opacity": 1 },
      },
      {
        id: "basemap-opentopo",
        type: "raster",
        source: "opentopomap",
        layout: { visibility: "visible" },
        paint: { "raster-opacity": 0.94 },
      },
      {
        id: "basemap-satellite",
        type: "raster",
        source: "esri-imagery",
        layout: { visibility: "none" },
        paint: { "raster-opacity": 1 },
      },
      {
        id: "viewshed-coverage-fill",
        type: "fill",
        source: "viewshed-coverage",
        paint: { "fill-color": "#10B981", "fill-opacity": 0.28 },
      },
      {
        id: "viewshed-coverage-outline",
        type: "line",
        source: "viewshed-coverage",
        paint: {
          "line-color": "#34D399",
          "line-width": 1.2,
          "line-opacity": 0.78,
        },
      },
      {
        id: "viewshed-rings-line",
        type: "line",
        source: "viewshed-rings",
        paint: {
          "line-color": "#10B981",
          "line-width": 1.5,
          "line-dasharray": [5, 4],
          "line-opacity": 0.8,
        },
      },
      {
        id: "sheet-footprint-fill",
        type: "fill",
        source: "sheet-footprint",
        paint: { "fill-color": "#F59E0B", "fill-opacity": 0.1 },
      },
      {
        id: "mgrs-grid-casing",
        type: "line",
        source: "mgrs-grid",
        paint: {
          "line-color": "#0F1419",
          "line-width": 2.4,
          "line-opacity": 0.5,
        },
      },
      {
        id: "mgrs-grid-lines",
        type: "line",
        source: "mgrs-grid",
        paint: {
          "line-color": [
            "case",
            ["==", ["get", "is_100km"], true],
            "#FBBF24",
            "#38BDF8",
          ],
          "line-width": [
            "case",
            ["==", ["get", "is_100km"], true],
            2.1,
            1.1,
          ],
          "line-opacity": [
            "case",
            ["==", ["get", "is_100km"], true],
            0.95,
            0.82,
          ],
        },
      },
      {
        id: "sheet-footprint-casing",
        type: "line",
        source: "sheet-footprint",
        paint: {
          "line-color": "#0F1419",
          "line-width": 4.5,
          "line-opacity": 0.72,
        },
      },
      {
        id: "sheet-footprint-outline",
        type: "line",
        source: "sheet-footprint",
        paint: {
          "line-color": "#F59E0B",
          "line-width": 2.4,
          "line-dasharray": [5, 3],
          "line-opacity": 0.98,
        },
      },
      // Tactical Sectors (AOI polygons + draft)
      {
        id: "tactical-sectors-fill",
        type: "fill",
        source: "tactical-sectors",
        paint: {
          "fill-color": ["coalesce", ["get", "color"], "#F59E0B"],
          "fill-opacity": ["case", ["==", ["get", "draft"], true], 0.14, 0.2],
        },
      },
      {
        id: "tactical-sectors-outline",
        type: "line",
        source: "tactical-sectors",
        paint: {
          "line-color": ["coalesce", ["get", "color"], "#F59E0B"],
          "line-width": 2.2,
          "line-dasharray": [4, 2],
          "line-opacity": 0.94,
        },
      },
      // Tactical Range Rings
      {
        id: "tactical-rings-casing",
        type: "line",
        source: "tactical-rings",
        paint: {
          "line-color": "#0F1419",
          "line-width": 3.4,
          "line-opacity": 0.65,
        },
      },
      {
        id: "tactical-rings-line",
        type: "line",
        source: "tactical-rings",
        paint: {
          "line-color": ["coalesce", ["get", "color"], "#38BDF8"],
          "line-width": 1.8,
          "line-dasharray": [5, 3],
          "line-opacity": 0.92,
        },
      },
      // Tactical Bearing Lines & Corridors
      {
        id: "tactical-lines-casing",
        type: "line",
        source: "tactical-lines",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#0F1419",
          "line-width": 4.4,
          "line-opacity": 0.78,
        },
      },
      {
        id: "tactical-lines-line",
        type: "line",
        source: "tactical-lines",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["coalesce", ["get", "color"], "#F97316"],
          "line-width": 2.4,
          "line-opacity": 0.96,
        },
      },
      // RF Point-to-Point Links
      {
        id: "rf-links-casing",
        type: "line",
        source: "rf-links-lines",
        layout: { "line-cap": "round" },
        paint: {
          "line-color": "#0F1419",
          "line-width": 5.0,
          "line-opacity": 0.82,
        },
      },
      {
        id: "rf-links-line",
        type: "line",
        source: "rf-links-lines",
        layout: { "line-cap": "round" },
        paint: {
          "line-color": ["coalesce", ["get", "color"], "#38BDF8"],
          "line-width": 2.6,
          "line-dasharray": [6, 3],
          "line-opacity": 0.98,
        },
      },
      // Live Ruler Polyline & Vertices
      {
        id: "ruler-line-casing",
        type: "line",
        source: "ruler-line",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#0F1419",
          "line-width": 5.0,
          "line-opacity": 0.84,
        },
      },
      {
        id: "ruler-line-stroke",
        type: "line",
        source: "ruler-line",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#38BDF8",
          "line-width": 2.6,
          "line-dasharray": [4, 3],
          "line-opacity": 0.98,
        },
      },
      {
        id: "ruler-points-circle",
        type: "circle",
        source: "ruler-points",
        paint: {
          "circle-radius": 5,
          "circle-color": "#0F1419",
          "circle-stroke-width": 2.2,
          "circle-stroke-color": "#38BDF8",
        },
      },
      // Strip Route
      {
        id: "strip-route-casing",
        type: "line",
        source: "strip-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#0F1419",
          "line-width": 5.2,
          "line-opacity": 0.8,
        },
      },
      {
        id: "strip-route-line",
        type: "line",
        source: "strip-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#EC4899",
          "line-width": 3.0,
          "line-opacity": 0.96,
        },
      },
      // Waypoints
      {
        id: "waypoints-legs-casing",
        type: "line",
        source: "waypoints-legs",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#0F1419",
          "line-width": 4.4,
          "line-opacity": 0.75,
        },
      },
      {
        id: "waypoints-legs-line",
        type: "line",
        source: "waypoints-legs",
        paint: {
          "line-color": "#E879F9",
          "line-width": 2.4,
          "line-dasharray": [4, 3],
          "line-opacity": 0.96,
        },
      },
      {
        id: "sheet-labels-layer",
        type: "symbol",
        source: "sheet-labels",
        layout: {
          "icon-image": ["get", "icon_id"],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      },
      {
        id: "tactical-pins-layer",
        type: "symbol",
        source: "tactical-pins",
        layout: {
          "icon-image": ["get", "icon_id"],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-anchor": "center",
        },
      },
      {
        id: "waypoints-points-layer",
        type: "symbol",
        source: "waypoints-points",
        layout: {
          "icon-image": ["get", "icon_id"],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-anchor": "bottom",
        },
      },
      {
        id: "viewshed-observers-layer",
        type: "symbol",
        source: "viewshed-observers",
        layout: {
          "icon-image": ["get", "icon_id"],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-anchor": "bottom",
        },
      },
      {
        id: "rf-links-pins-layer",
        type: "symbol",
        source: "rf-links-pins",
        layout: {
          "icon-image": ["get", "icon_id"],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-anchor": "bottom",
        },
      },
    ],
  };
}

function createReticleElement(): HTMLDivElement {
  const el = document.createElement("div");
  el.className = "gf-center-reticle";
  el.title = "Drag to reposition sheet center";
  el.innerHTML = `
    <svg width="34" height="34" viewBox="0 0 34 34" fill="none" aria-hidden="true">
      <circle cx="17" cy="17" r="11" stroke="#0F1419" stroke-width="4" opacity="0.75"/>
      <circle cx="17" cy="17" r="11" stroke="#F59E0B" stroke-width="1.8"/>
      <line x1="17" y1="2" x2="17" y2="11" stroke="#0F1419" stroke-width="3.4" opacity="0.75"/>
      <line x1="17" y1="2" x2="17" y2="11" stroke="#F59E0B" stroke-width="1.8"/>
      <line x1="17" y1="23" x2="17" y2="32" stroke="#0F1419" stroke-width="3.4" opacity="0.75"/>
      <line x1="17" y1="23" x2="17" y2="32" stroke="#F59E0B" stroke-width="1.8"/>
      <line x1="2" y1="17" x2="11" y2="17" stroke="#0F1419" stroke-width="3.4" opacity="0.75"/>
      <line x1="2" y1="17" x2="11" y2="17" stroke="#F59E0B" stroke-width="1.8"/>
      <line x1="23" y1="17" x2="32" y2="17" stroke="#0F1419" stroke-width="3.4" opacity="0.75"/>
      <line x1="23" y1="17" x2="32" y2="17" stroke="#F59E0B" stroke-width="1.8"/>
      <circle cx="17" cy="17" r="2.6" fill="#F59E0B" stroke="#0F1419" stroke-width="1"/>
    </svg>
  `;
  return el;
}

function renderSheetBadgeImage(
  label: string,
  accentColor = "#F59E0B",
  textColor = "#FBBF24"
): {
  width: number;
  height: number;
  data: Uint8Array;
} | null {
  const scale = 2;
  const logicalW = Math.max(36, 14 + label.length * 7.2);
  const w = Math.ceil(logicalW * scale);
  const h = 22 * scale;
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const r = 5 * scale;
  const inset = 1.5 * scale;
  ctx.beginPath();
  ctx.roundRect(inset, inset, w - inset * 2, h - inset * 2, r);
  ctx.fillStyle = "rgba(15, 20, 25, 0.9)";
  ctx.fill();
  ctx.lineWidth = 1.5 * scale;
  ctx.strokeStyle = accentColor;
  ctx.stroke();

  ctx.fillStyle = textColor;
  ctx.font = `700 ${10.5 * scale}px "JetBrains Mono", "SF Mono", ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, w / 2, h / 2 + 0.5 * scale);

  const img = ctx.getImageData(0, 0, w, h);
  return { width: w, height: h, data: new Uint8Array(img.data.buffer) };
}

function renderPinBadgeImage(
  code: string,
  accentColor: string,
  withRadarDot = false
): {
  width: number;
  height: number;
  data: Uint8Array;
} | null {
  const scale = 2;
  const extraLeft = withRadarDot ? 11 : 0;
  const logicalW = Math.max(40, 16 + extraLeft + code.length * 7.4);
  const badgeH = 22;
  const tipH = 7;
  const w = Math.ceil(logicalW * scale);
  const h = (badgeH + tipH) * scale;
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const inset = 1.5 * scale;
  const bh = badgeH * scale;
  const r = 5 * scale;

  ctx.beginPath();
  ctx.moveTo(w / 2 - 5 * scale, bh - 2 * scale);
  ctx.lineTo(w / 2, h - 1 * scale);
  ctx.lineTo(w / 2 + 5 * scale, bh - 2 * scale);
  ctx.closePath();
  ctx.fillStyle = accentColor;
  ctx.fill();

  ctx.beginPath();
  ctx.roundRect(inset, inset, w - inset * 2, bh - inset * 1.5, r);
  ctx.fillStyle = "rgba(15, 20, 25, 0.94)";
  ctx.fill();
  ctx.lineWidth = 1.8 * scale;
  ctx.strokeStyle = accentColor;
  ctx.stroke();

  if (withRadarDot) {
    const cx = 10.5 * scale;
    const cy = bh / 2;
    ctx.beginPath();
    ctx.arc(cx, cy, 4 * scale, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(16, 185, 129, 0.28)";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, cy, 2.2 * scale, 0, Math.PI * 2);
    ctx.fillStyle = accentColor;
    ctx.fill();
  }

  ctx.fillStyle = "#F8FAFC";
  ctx.font = `700 ${10.5 * scale}px "JetBrains Mono", "SF Mono", ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(code, w / 2 + (extraLeft * scale) / 2, bh / 2 + 0.5 * scale);

  const img = ctx.getImageData(0, 0, w, h);
  return { width: w, height: h, data: new Uint8Array(img.data.buffer) };
}

function geodesicCircleRing(
  centerLat: number,
  centerLon: number,
  radiusKm: number,
  segments = 72
): Position[] {
  const R = 6371.0088;
  const angularDist = radiusKm / R;
  const lat1 = (centerLat * Math.PI) / 180;
  const lon1 = (centerLon * Math.PI) / 180;
  const coords: Position[] = [];
  for (let i = 0; i <= segments; i += 1) {
    const bearing = (2 * Math.PI * i) / segments;
    const sinLat2 =
      Math.sin(lat1) * Math.cos(angularDist) +
      Math.cos(lat1) * Math.sin(angularDist) * Math.cos(bearing);
    const lat2 = Math.asin(Math.max(-1, Math.min(1, sinLat2)));
    const lon2 =
      lon1 +
      Math.atan2(
        Math.sin(bearing) * Math.sin(angularDist) * Math.cos(lat1),
        Math.cos(angularDist) - Math.sin(lat1) * Math.sin(lat2)
      );
    coords.push([(lon2 * 180) / Math.PI, (lat2 * 180) / Math.PI]);
  }
  return coords;
}

function polygonCentroid(ring: Position[]): [number, number] | null {
  if (ring.length === 0) return null;
  const count =
    ring.length > 1 &&
    ring[0]?.[0] === ring[ring.length - 1]?.[0] &&
    ring[0]?.[1] === ring[ring.length - 1]?.[1]
      ? ring.length - 1
      : ring.length;
  if (count <= 0) return null;

  let sumX = 0;
  let sumY = 0;
  for (let i = 0; i < count; i += 1) {
    const p = ring[i];
    if (!p || typeof p[0] !== "number" || typeof p[1] !== "number") continue;
    sumX += p[0];
    sumY += p[1];
  }
  return [sumX / count, sumY / count];
}

function buildSheetLabelsGeoJson(
  footprint: FeatureCollection
): { fc: FeatureCollection<Point>; labels: string[] } {
  if (footprint.features.length <= 1) {
    return { fc: { type: "FeatureCollection", features: [] }, labels: [] };
  }

  const labelFeatures: Feature<Point>[] = [];
  const labels: string[] = [];

  for (let idx = 0; idx < footprint.features.length; idx += 1) {
    const feat = footprint.features[idx];
    if (!feat || !feat.geometry || feat.geometry.type !== "Polygon") continue;
    const outerRing = feat.geometry.coordinates[0];
    if (!outerRing) continue;
    const centroid = polygonCentroid(outerRing);
    if (!centroid) continue;

    const props = (feat.properties ?? {}) as Record<string, unknown>;
    const rawLabel =
      typeof props.label === "string"
        ? props.label
        : typeof props.sheet_id === "string"
          ? props.sheet_id
          : `S${idx + 1}`;
    const label = rawLabel.trim().toUpperCase().slice(0, 5);
    const iconId = `sheet-label-${label}`;
    labels.push(label);

    labelFeatures.push({
      type: "Feature",
      properties: { label, icon_id: iconId },
      geometry: { type: "Point", coordinates: centroid },
    });
  }

  return {
    fc: { type: "FeatureCollection", features: labelFeatures },
    labels,
  };
}

function collectPositions(fc: FeatureCollection): Position[] {
  const pts: Position[] = [];
  for (const feature of fc.features) {
    const geom = feature.geometry;
    if (!geom) continue;
    if (geom.type === "Point") {
      pts.push(geom.coordinates);
    } else if (geom.type === "LineString" || geom.type === "MultiPoint") {
      for (const p of geom.coordinates) pts.push(p);
    } else if (geom.type === "Polygon" || geom.type === "MultiLineString") {
      for (const ring of geom.coordinates) {
        for (const p of ring) pts.push(p);
      }
    } else if (geom.type === "MultiPolygon") {
      for (const poly of geom.coordinates) {
        for (const ring of poly) {
          for (const p of ring) pts.push(p);
        }
      }
    }
  }
  return pts;
}

function rfStatusColor(entry: RFLinkEntry): string {
  if (entry.loading) return "#38BDF8";
  if (entry.error) return "#EF4444";
  const st = entry.profile?.status;
  if (st === "CLEAR") return "#10B981";
  if (st === "MARGINAL") return "#F59E0B";
  if (st === "OBSTRUCTED") return "#EF4444";
  return "#38BDF8";
}

export class GridForgeMap {
  private readonly container: HTMLElement;
  private readonly map: maplibregl.Map;
  private readonly marker: maplibregl.Marker;
  private isLoaded = false;
  private pendingFootprint: FeatureCollection | null = null;
  private pendingGrid: FeatureCollection | null = null;
  private pendingStripRoute: FeatureCollection | null = null;
  private pendingWaypoints: WaypointInput[] | null = null;
  private pendingViewsheds: ViewshedEntry[] | null = null;
  private pendingTactical: {
    graphics: TacticalGraphic[];
    sectorDraft: [number, number][];
  } | null = null;
  private pendingRFLinks: {
    links: RFLinkEntry[];
    pendingFrom: { lat: number; lon: number; label: string } | null;
  } | null = null;
  private pendingRuler: { lat: number; lon: number }[] | null = null;
  private pendingOfflineBasemap: BasemapInfoResponse | null = null;

  private offlineBasemapReady = false;
  private offlineBasemapKey = "";
  private reportedExternalTileFail = false;
  private currentFootprint: FeatureCollection | null = null;
  private currentBasemap: BasemapId = "opentopo";
  private suppressClickUntil = 0;
  private draggingMarker = false;
  private interactionMode: MapInteractionMode = "pan";

  constructor(
    container: HTMLElement,
    callbacks: GridForgeMapCallbacks,
    options?: GridForgeMapOptions
  ) {
    this.container = container;
    const initLat = options?.initialLat ?? DEFAULT_CENTER_LAT;
    const initLon = options?.initialLon ?? DEFAULT_CENTER_LON;
    const initZoom = options?.initialZoom ?? DEFAULT_MAP_ZOOM;

    this.map = new maplibregl.Map({
      container,
      style: buildInitialStyle(),
      center: [initLon, initLat],
      zoom: initZoom,
      attributionControl: false,
      pitchWithRotate: false,
    });

    this.map.addControl(
      new maplibregl.AttributionControl({ compact: true }),
      "bottom-right"
    );
    this.map.addControl(
      new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }),
      "bottom-left"
    );

    this.marker = new maplibregl.Marker({
      element: createReticleElement(),
      draggable: true,
      anchor: "center",
    })
      .setLngLat([initLon, initLat])
      .addTo(this.map);

    this.marker.on("dragstart", () => {
      this.draggingMarker = true;
    });

    this.marker.on("drag", () => {
      const pos = this.marker.getLngLat();
      callbacks.onCenterDrag(pos.lat, pos.lng);
    });

    this.marker.on("dragend", () => {
      this.draggingMarker = false;
      this.suppressClickUntil = performance.now() + 200;
      const pos = this.marker.getLngLat();
      callbacks.onCenterCommit(pos.lat, pos.lng);
    });

    this.map.on("click", (e) => {
      if (performance.now() < this.suppressClickUntil) return;
      const { lat, lng } = e.lngLat;
      if (this.interactionMode === "los") {
        callbacks.onMapViewshedClick?.(lat, lng);
        return;
      }
      if (this.interactionMode === "markup") {
        callbacks.onMapMarkupClick?.(lat, lng);
        return;
      }
      if (this.interactionMode === "rf-link") {
        callbacks.onMapRFLinkClick?.(lat, lng);
        return;
      }
      if (this.interactionMode === "ruler") {
        callbacks.onMapRulerClick?.(lat, lng);
        return;
      }
      if (this.interactionMode === "ring") {
        callbacks.onMapRingClick?.(lat, lng);
        return;
      }
      if (this.interactionMode === "sector") {
        callbacks.onMapSectorClick?.(lat, lng);
        return;
      }
      this.marker.setLngLat([lng, lat]);
      callbacks.onCenterCommit(lat, lng);
    });

    this.map.on("dblclick", (e) => {
      if (this.interactionMode === "sector") {
        e.preventDefault();
        callbacks.onMapSectorFinish?.();
      }
    });

    this.map.on("mousemove", (e) => {
      callbacks.onCursorMove?.(e.lngLat.lat, e.lngLat.lng);
    });

    this.map.on("error", (ev) => {
      const srcId = (ev as { sourceId?: string }).sourceId;
      if (
        this.offlineBasemapReady &&
        !this.reportedExternalTileFail &&
        this.currentBasemap !== "offline" &&
        srcId &&
        EXTERNAL_SOURCE_IDS.has(srcId)
      ) {
        this.reportedExternalTileFail = true;
        callbacks.onExternalTileFailure?.();
      }
    });

    this.map.on("load", () => {
      this.isLoaded = true;
      if (this.pendingOfflineBasemap) {
        this.attachOfflineBasemapLayer(this.pendingOfflineBasemap);
        this.pendingOfflineBasemap = null;
      }
      this.applyBasemapVisibility();
      if (this.pendingFootprint || this.pendingGrid) {
        this.applyGeoJson(
          this.pendingFootprint ?? EMPTY_FC,
          this.pendingGrid ?? EMPTY_FC
        );
        this.pendingFootprint = null;
        this.pendingGrid = null;
      }
      if (this.pendingStripRoute) {
        this.applyStripRoute(this.pendingStripRoute);
        this.pendingStripRoute = null;
      }
      if (this.pendingWaypoints) {
        this.applyWaypoints(this.pendingWaypoints);
        this.pendingWaypoints = null;
      }
      if (this.pendingViewsheds) {
        this.applyViewsheds(this.pendingViewsheds);
        this.pendingViewsheds = null;
      }
      if (this.pendingTactical) {
        this.applyTacticalGraphics(
          this.pendingTactical.graphics,
          this.pendingTactical.sectorDraft
        );
        this.pendingTactical = null;
      }
      if (this.pendingRFLinks) {
        this.applyRFLinks(
          this.pendingRFLinks.links,
          this.pendingRFLinks.pendingFrom
        );
        this.pendingRFLinks = null;
      }
      if (this.pendingRuler) {
        this.applyRuler(this.pendingRuler);
        this.pendingRuler = null;
      }
    });
  }

  enableOfflineBasemap(info: BasemapInfoResponse): void {
    if (!this.isLoaded) {
      this.pendingOfflineBasemap = info;
      return;
    }
    this.attachOfflineBasemapLayer(info);
  }

  private removeOfflineBasemapLayer(): void {
    if (this.map.getLayer("basemap-offline")) {
      this.map.removeLayer("basemap-offline");
    }
    if (this.map.getSource("offline-mbtiles")) {
      this.map.removeSource("offline-mbtiles");
    }
    this.offlineBasemapReady = false;
    this.offlineBasemapKey = "";
  }

  private attachOfflineBasemapLayer(info: BasemapInfoResponse): void {
    if (!info.available || !info.format) {
      this.removeOfflineBasemapLayer();
      if (this.currentBasemap === "offline") {
        this.currentBasemap = "opentopo";
      }
      this.applyBasemapVisibility();
      return;
    }

    const ext = info.format === "jpg" ? "jpg" : "png";
    const qs = info.pack_id
      ? `?pack_id=${encodeURIComponent(info.pack_id)}`
      : "";
    const tileUrl = `/api/basemap/{z}/{x}/{y}.${ext}${qs}`;
    const key = `${tileUrl}|${JSON.stringify(info.bounds ?? null)}|${info.minzoom}|${info.maxzoom}`;

    if (this.offlineBasemapKey === key && this.map.getSource("offline-mbtiles")) {
      this.offlineBasemapReady = true;
      this.applyBasemapVisibility();
      return;
    }

    this.removeOfflineBasemapLayer();

    const sourceSpec: maplibregl.RasterSourceSpecification = {
      type: "raster",
      tiles: [tileUrl],
      tileSize: 256,
      minzoom: info.minzoom ?? 0,
      maxzoom: info.maxzoom ?? 18,
      attribution: info.attribution || info.name || "Local Offline MBTiles Basemap",
    };
    if (info.bounds && info.bounds.length === 4) {
      sourceSpec.bounds = info.bounds as [number, number, number, number];
    }

    this.map.addSource("offline-mbtiles", sourceSpec);
    this.map.addLayer(
      {
        id: "basemap-offline",
        type: "raster",
        source: "offline-mbtiles",
        layout: {
          visibility: this.currentBasemap === "offline" ? "visible" : "none",
        },
        paint: { "raster-opacity": 1 },
      },
      "viewshed-coverage-fill"
    );
    this.offlineBasemapReady = true;
    this.offlineBasemapKey = key;
    this.applyBasemapVisibility();
  }

  setInteractionMode(mode: MapInteractionMode): void {
    this.interactionMode = mode;
    this.container.classList.toggle("tool-active", mode !== "pan");
    this.container.classList.toggle("markup-active", mode === "markup");
    this.container.classList.toggle("los-active", mode === "los");
    this.container.classList.toggle("rf-link-active", mode === "rf-link");
    this.container.classList.toggle("ruler-active", mode === "ruler");
    this.container.classList.toggle("ring-active", mode === "ring");
    this.container.classList.toggle("sector-active", mode === "sector");

    if (mode === "sector" || mode === "ruler") {
      this.map.doubleClickZoom.disable();
    } else {
      this.map.doubleClickZoom.enable();
    }
  }

  setBasemap(basemap: BasemapId): void {
    this.currentBasemap = basemap;
    if (this.isLoaded) {
      this.applyBasemapVisibility();
    }
  }

  private applyBasemapVisibility(): void {
    const showOsm =
      this.currentBasemap === "osm" || this.currentBasemap === "opentopo";
    const showOpenTopo = this.currentBasemap === "opentopo";
    const showSat = this.currentBasemap === "satellite";
    const showOffline =
      this.currentBasemap === "offline" && this.offlineBasemapReady;

    this.map.setLayoutProperty(
      "basemap-osm-underlay",
      "visibility",
      showOsm ? "visible" : "none"
    );
    this.map.setLayoutProperty(
      "basemap-opentopo",
      "visibility",
      showOpenTopo ? "visible" : "none"
    );
    this.map.setLayoutProperty(
      "basemap-satellite",
      "visibility",
      showSat ? "visible" : "none"
    );
    if (this.map.getLayer("basemap-offline")) {
      this.map.setLayoutProperty(
        "basemap-offline",
        "visibility",
        showOffline ? "visible" : "none"
      );
    }
  }

  private ensureSheetLabelImages(labels: string[]): void {
    for (const label of labels) {
      const iconId = `sheet-label-${label}`;
      if (this.map.hasImage(iconId)) continue;
      const rendered = renderSheetBadgeImage(label);
      if (rendered) {
        this.map.addImage(iconId, rendered, { pixelRatio: 2 });
      }
    }
  }

  private ensureBadgeImage(
    iconId: string,
    label: string,
    accentColor: string,
    textColor = "#F8FAFC"
  ): string {
    if (!this.map.hasImage(iconId)) {
      const rendered = renderSheetBadgeImage(label, accentColor, textColor);
      if (rendered) {
        this.map.addImage(iconId, rendered, { pixelRatio: 2 });
      }
    }
    return iconId;
  }

  private ensureWaypointPinImage(
    code: string,
    category: WaypointCategory
  ): string {
    const iconId = `wp-pin-${code}-${category}`;
    if (!this.map.hasImage(iconId)) {
      const color = CATEGORY_COLOR_MAP[category] ?? "#EC4899";
      const rendered = renderPinBadgeImage(code, color, false);
      if (rendered) {
        this.map.addImage(iconId, rendered, { pixelRatio: 2 });
      }
    }
    return iconId;
  }

  private ensureObserverPinImage(code: string): string {
    const iconId = `obs-pin-${code}`;
    if (!this.map.hasImage(iconId)) {
      const rendered = renderPinBadgeImage(code, "#10B981", true);
      if (rendered) {
        this.map.addImage(iconId, rendered, { pixelRatio: 2 });
      }
    }
    return iconId;
  }

  private ensureRFPinImage(code: string, color: string): string {
    const safeColor = color.replace("#", "");
    const iconId = `rf-pin-${code}-${safeColor}`;
    if (!this.map.hasImage(iconId)) {
      const rendered = renderPinBadgeImage(code, color, false);
      if (rendered) {
        this.map.addImage(iconId, rendered, { pixelRatio: 2 });
      }
    }
    return iconId;
  }

  isDragging(): boolean {
    return this.draggingMarker;
  }

  setCenter(
    lat: number,
    lon: number,
    options?: { fly?: boolean; zoom?: number }
  ): void {
    this.marker.setLngLat([lon, lat]);
    if (options?.fly) {
      this.map.flyTo({
        center: [lon, lat],
        zoom: options.zoom ?? Math.max(this.map.getZoom(), 11.5),
        duration: 650,
        essential: true,
      });
    }
  }

  updateFootprint(
    footprint: FeatureCollection,
    grid: FeatureCollection,
    showGridLines = true
  ): void {
    this.currentFootprint = footprint;
    const gridData = showGridLines ? grid : EMPTY_FC;
    if (!this.isLoaded) {
      this.pendingFootprint = footprint;
      this.pendingGrid = gridData;
      return;
    }
    this.applyGeoJson(footprint, gridData);
  }

  clearFootprint(): void {
    this.updateFootprint(EMPTY_FC, EMPTY_FC);
  }

  updateStripRoute(routeGeoJson: FeatureCollection | null): void {
    const fc = routeGeoJson ?? EMPTY_FC;
    if (!this.isLoaded) {
      this.pendingStripRoute = fc;
      return;
    }
    this.applyStripRoute(fc);
  }

  updateWaypoints(waypoints: WaypointInput[]): void {
    if (!this.isLoaded) {
      this.pendingWaypoints = [...waypoints];
      return;
    }
    this.applyWaypoints(waypoints);
  }

  updateViewsheds(entries: ViewshedEntry[]): void {
    if (!this.isLoaded) {
      this.pendingViewsheds = [...entries];
      return;
    }
    this.applyViewsheds(entries);
  }

  updateTacticalGraphics(
    graphics: TacticalGraphic[],
    sectorDraft: [number, number][] = []
  ): void {
    if (!this.isLoaded) {
      this.pendingTactical = {
        graphics: [...graphics],
        sectorDraft: [...sectorDraft],
      };
      return;
    }
    this.applyTacticalGraphics(graphics, sectorDraft);
  }

  updateRFLinks(
    links: RFLinkEntry[],
    pendingFrom: { lat: number; lon: number; label: string } | null = null
  ): void {
    if (!this.isLoaded) {
      this.pendingRFLinks = { links: [...links], pendingFrom };
      return;
    }
    this.applyRFLinks(links, pendingFrom);
  }

  updateRuler(points: { lat: number; lon: number }[]): void {
    if (!this.isLoaded) {
      this.pendingRuler = [...points];
      return;
    }
    this.applyRuler(points);
  }

  getFootprintBBoxString(): string | null {
    if (!this.currentFootprint) return null;
    const pts = collectPositions(this.currentFootprint);
    if (pts.length === 0) return null;
    let minLon = Infinity;
    let minLat = Infinity;
    let maxLon = -Infinity;
    let maxLat = -Infinity;
    for (const p of pts) {
      const lon = p[0];
      const lat = p[1];
      if (typeof lon === "number" && typeof lat === "number") {
        if (lon < minLon) minLon = lon;
        if (lon > maxLon) maxLon = lon;
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
      }
    }
    if (!Number.isFinite(minLon)) return null;
    const pad = 0.05;
    return `${(minLon - pad).toFixed(4)},${(minLat - pad).toFixed(4)},${(maxLon + pad).toFixed(4)},${(maxLat + pad).toFixed(4)}`;
  }

  private applyGeoJson(
    footprint: FeatureCollection,
    grid: FeatureCollection
  ): void {
    const fpSource = this.map.getSource("sheet-footprint") as
      | maplibregl.GeoJSONSource
      | undefined;
    const gridSource = this.map.getSource("mgrs-grid") as
      | maplibregl.GeoJSONSource
      | undefined;
    const labelSource = this.map.getSource("sheet-labels") as
      | maplibregl.GeoJSONSource
      | undefined;

    const { fc: labelsFc, labels } = buildSheetLabelsGeoJson(footprint);
    this.ensureSheetLabelImages(labels);

    fpSource?.setData(footprint);
    gridSource?.setData(grid);
    labelSource?.setData(labelsFc);
  }

  private applyStripRoute(routeGeoJson: FeatureCollection): void {
    const src = this.map.getSource("strip-route") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(routeGeoJson);
  }

  private applyWaypoints(waypoints: WaypointInput[]): void {
    const pointFeatures: Feature<Point>[] = [];
    const legCoords: Position[] = [];

    for (let idx = 0; idx < waypoints.length; idx += 1) {
      const wp = waypoints[idx];
      if (!wp) continue;
      const code = `WP${idx + 1}`;
      const iconId = this.ensureWaypointPinImage(code, wp.category);
      pointFeatures.push({
        type: "Feature",
        properties: { code, label: wp.label, category: wp.category, icon_id: iconId },
        geometry: { type: "Point", coordinates: [wp.lon, wp.lat] },
      });
      legCoords.push([wp.lon, wp.lat]);
    }

    const legsFc: FeatureCollection<LineString> = {
      type: "FeatureCollection",
      features:
        legCoords.length >= 2
          ? [
              {
                type: "Feature",
                properties: {},
                geometry: { type: "LineString", coordinates: legCoords },
              },
            ]
          : [],
    };

    const pointsFc: FeatureCollection<Point> = {
      type: "FeatureCollection",
      features: pointFeatures,
    };

    (
      this.map.getSource("waypoints-legs") as maplibregl.GeoJSONSource | undefined
    )?.setData(legsFc);
    (
      this.map.getSource("waypoints-points") as
        | maplibregl.GeoJSONSource
        | undefined
    )?.setData(pointsFc);
  }

  private applyViewsheds(entries: ViewshedEntry[]): void {
    const coverageFeatures: Feature[] = [];
    const ringFeatures: Feature[] = [];
    const observerFeatures: Feature<Point>[] = [];

    for (let idx = 0; idx < entries.length; idx += 1) {
      const entry = entries[idx];
      if (!entry) continue;
      const code = `OBS${idx + 1}`;
      const iconId = this.ensureObserverPinImage(code);
      observerFeatures.push({
        type: "Feature",
        properties: { code, label: entry.observer.label, icon_id: iconId },
        geometry: {
          type: "Point",
          coordinates: [entry.observer.lon, entry.observer.lat],
        },
      });
      if (entry.preview?.viewshed_geojson?.features) {
        coverageFeatures.push(...entry.preview.viewshed_geojson.features);
      }
      if (entry.preview?.range_ring_geojson?.features) {
        ringFeatures.push(...entry.preview.range_ring_geojson.features);
      }
    }

    (
      this.map.getSource("viewshed-coverage") as
        | maplibregl.GeoJSONSource
        | undefined
    )?.setData({ type: "FeatureCollection", features: coverageFeatures });
    (
      this.map.getSource("viewshed-rings") as maplibregl.GeoJSONSource | undefined
    )?.setData({ type: "FeatureCollection", features: ringFeatures });
    (
      this.map.getSource("viewshed-observers") as
        | maplibregl.GeoJSONSource
        | undefined
    )?.setData({ type: "FeatureCollection", features: observerFeatures });
  }

  private applyTacticalGraphics(
    graphics: TacticalGraphic[],
    sectorDraft: [number, number][]
  ): void {
    const sectorFeatures: Feature<Polygon | LineString>[] = [];
    const ringFeatures: Feature<LineString>[] = [];
    const lineFeatures: Feature<LineString>[] = [];
    const pinFeatures: Feature<Point>[] = [];

    graphics.forEach((g, idx) => {
      const shortLabel = (g.label || `TG${idx + 1}`).trim().slice(0, 14);
      if (g.kind === "range_ring" && g.points.length >= 1) {
        const [lat, lon] = g.points[0]!;
        const color = g.color || "#38BDF8";
        for (const rKm of g.radii_km) {
          if (rKm <= 0) continue;
          ringFeatures.push({
            type: "Feature",
            properties: { label: shortLabel, radius_km: rKm, color },
            geometry: {
              type: "LineString",
              coordinates: geodesicCircleRing(lat, lon, rKm),
            },
          });
        }
        const iconId = this.ensureBadgeImage(
          `tac-ring-${idx}-${shortLabel}`,
          shortLabel,
          color,
          "#7DD3FC"
        );
        pinFeatures.push({
          type: "Feature",
          properties: { icon_id: iconId },
          geometry: { type: "Point", coordinates: [lon, lat] },
        });
      } else if (g.kind === "sector" && g.points.length >= 3) {
        const color = g.color || "#F59E0B";
        const coords: Position[] = g.points.map(([lat, lon]) => [lon, lat]);
        coords.push(coords[0]!);
        sectorFeatures.push({
          type: "Feature",
          properties: { label: shortLabel, color, draft: false },
          geometry: { type: "Polygon", coordinates: [coords] },
        });
        const centroid = polygonCentroid(coords);
        if (centroid) {
          const iconId = this.ensureBadgeImage(
            `tac-sec-${idx}-${shortLabel}`,
            shortLabel,
            color,
            "#FBBF24"
          );
          pinFeatures.push({
            type: "Feature",
            properties: { icon_id: iconId },
            geometry: { type: "Point", coordinates: centroid },
          });
        }
      } else if (
        (g.kind === "bearing_line" || g.kind === "corridor") &&
        g.points.length >= 2
      ) {
        const color = g.color || "#F97316";
        const coords: Position[] = g.points.map(([lat, lon]) => [lon, lat]);
        lineFeatures.push({
          type: "Feature",
          properties: { label: shortLabel, color },
          geometry: { type: "LineString", coordinates: coords },
        });
        const mid = coords[Math.floor(coords.length / 2)]!;
        const prev = coords[Math.max(0, Math.floor(coords.length / 2) - 1)]!;
        const midPt: [number, number] = [
          (mid[0]! + prev[0]!) / 2,
          (mid[1]! + prev[1]!) / 2,
        ];
        const iconId = this.ensureBadgeImage(
          `tac-line-${idx}-${shortLabel}`,
          shortLabel,
          color,
          "#FDBA74"
        );
        pinFeatures.push({
          type: "Feature",
          properties: { icon_id: iconId },
          geometry: { type: "Point", coordinates: midPt },
        });
      }
    });

    // In-progress sector AOI draft
    if (sectorDraft.length >= 3) {
      const coords: Position[] = sectorDraft.map(([lat, lon]) => [lon, lat]);
      coords.push(coords[0]!);
      sectorFeatures.push({
        type: "Feature",
        properties: { color: "#FBBF24", draft: true },
        geometry: { type: "Polygon", coordinates: [coords] },
      });
    } else if (sectorDraft.length === 2) {
      const coords: Position[] = sectorDraft.map(([lat, lon]) => [lon, lat]);
      lineFeatures.push({
        type: "Feature",
        properties: { color: "#FBBF24" },
        geometry: { type: "LineString", coordinates: coords },
      });
    } else if (sectorDraft.length === 1) {
      const [lat, lon] = sectorDraft[0]!;
      const iconId = this.ensureBadgeImage(
        "tac-sec-draft-pt",
        "AOI 1",
        "#FBBF24",
        "#FBBF24"
      );
      pinFeatures.push({
        type: "Feature",
        properties: { icon_id: iconId },
        geometry: { type: "Point", coordinates: [lon, lat] },
      });
    }

    (
      this.map.getSource("tactical-sectors") as
        | maplibregl.GeoJSONSource
        | undefined
    )?.setData({ type: "FeatureCollection", features: sectorFeatures });
    (
      this.map.getSource("tactical-rings") as maplibregl.GeoJSONSource | undefined
    )?.setData({ type: "FeatureCollection", features: ringFeatures });
    (
      this.map.getSource("tactical-lines") as maplibregl.GeoJSONSource | undefined
    )?.setData({ type: "FeatureCollection", features: lineFeatures });
    (
      this.map.getSource("tactical-pins") as maplibregl.GeoJSONSource | undefined
    )?.setData({ type: "FeatureCollection", features: pinFeatures });
  }

  private applyRFLinks(
    links: RFLinkEntry[],
    pendingFrom: { lat: number; lon: number; label: string } | null
  ): void {
    const lineFeatures: Feature<LineString>[] = [];
    const pinFeatures: Feature<Point>[] = [];

    links.forEach((entry, idx) => {
      const s = entry.spec;
      const color = rfStatusColor(entry);
      const fromCode = (s.from_label || `A${idx + 1}`).slice(0, 6);
      const toCode = (s.to_label || `B${idx + 1}`).slice(0, 6);

      lineFeatures.push({
        type: "Feature",
        properties: { id: entry.id, color },
        geometry: {
          type: "LineString",
          coordinates: [
            [s.from_lon, s.from_lat],
            [s.to_lon, s.to_lat],
          ],
        },
      });

      pinFeatures.push({
        type: "Feature",
        properties: { icon_id: this.ensureRFPinImage(fromCode, color) },
        geometry: { type: "Point", coordinates: [s.from_lon, s.from_lat] },
      });
      pinFeatures.push({
        type: "Feature",
        properties: { icon_id: this.ensureRFPinImage(toCode, color) },
        geometry: { type: "Point", coordinates: [s.to_lon, s.to_lat] },
      });
    });

    if (pendingFrom) {
      pinFeatures.push({
        type: "Feature",
        properties: {
          icon_id: this.ensureRFPinImage(pendingFrom.label, "#38BDF8"),
        },
        geometry: {
          type: "Point",
          coordinates: [pendingFrom.lon, pendingFrom.lat],
        },
      });
    }

    (
      this.map.getSource("rf-links-lines") as maplibregl.GeoJSONSource | undefined
    )?.setData({ type: "FeatureCollection", features: lineFeatures });
    (
      this.map.getSource("rf-links-pins") as maplibregl.GeoJSONSource | undefined
    )?.setData({ type: "FeatureCollection", features: pinFeatures });
  }

  private applyRuler(points: { lat: number; lon: number }[]): void {
    const coords: Position[] = points.map((p) => [p.lon, p.lat]);
    const lineFc: FeatureCollection<LineString> = {
      type: "FeatureCollection",
      features:
        coords.length >= 2
          ? [
              {
                type: "Feature",
                properties: {},
                geometry: { type: "LineString", coordinates: coords },
              },
            ]
          : [],
    };
    const ptsFc: FeatureCollection<Point> = {
      type: "FeatureCollection",
      features: coords.map((c, idx) => ({
        type: "Feature",
        properties: { idx },
        geometry: { type: "Point", coordinates: c },
      })),
    };
    (
      this.map.getSource("ruler-line") as maplibregl.GeoJSONSource | undefined
    )?.setData(lineFc);
    (
      this.map.getSource("ruler-points") as maplibregl.GeoJSONSource | undefined
    )?.setData(ptsFc);
  }

  fitFootprint(sidebarWidthPx = 368): void {
    if (!this.currentFootprint) return;
    const positions = collectPositions(this.currentFootprint);
    if (positions.length === 0) return;

    const bounds = new maplibregl.LngLatBounds();
    for (const pos of positions) {
      const lon = pos[0];
      const lat = pos[1];
      if (typeof lon === "number" && typeof lat === "number") {
        bounds.extend([lon, lat]);
      }
    }
    if (bounds.isEmpty()) return;

    this.map.fitBounds(bounds, {
      padding: {
        top: 84,
        bottom: 84,
        left: Math.min(
          sidebarWidthPx + 48,
          Math.floor(window.innerWidth * 0.44)
        ),
        right: 72,
      },
      duration: 500,
      maxZoom: 15,
    });
  }

  zoomIn(): void {
    this.map.zoomIn({ duration: 220 });
  }

  zoomOut(): void {
    this.map.zoomOut({ duration: 220 });
  }

  resetNorth(): void {
    this.map.resetNorth({ duration: 260 });
  }

  resize(): void {
    this.map.resize();
  }
}
