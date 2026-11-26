import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type {
  Feature,
  FeatureCollection,
  LineString,
  Point,
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
      "sheet-footprint": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "mgrs-grid": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "viewshed-coverage": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "viewshed-rings": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "strip-route": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "sheet-labels": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "waypoints-legs": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "waypoints-points": {
        type: "geojson",
        data: EMPTY_FC,
      },
      "viewshed-observers": {
        type: "geojson",
        data: EMPTY_FC,
      },
    },
    layers: [
      {
        id: "bg-dark",
        type: "background",
        paint: {
          "background-color": "#0F1419",
        },
      },
      {
        id: "basemap-osm-underlay",
        type: "raster",
        source: "osm-standard",
        layout: { visibility: "visible" },
        paint: {
          "raster-opacity": 1,
        },
      },
      {
        id: "basemap-opentopo",
        type: "raster",
        source: "opentopomap",
        layout: { visibility: "visible" },
        paint: {
          "raster-opacity": 0.94,
        },
      },
      {
        id: "basemap-satellite",
        type: "raster",
        source: "esri-imagery",
        layout: { visibility: "none" },
        paint: {
          "raster-opacity": 1,
        },
      },
      {
        id: "viewshed-coverage-fill",
        type: "fill",
        source: "viewshed-coverage",
        paint: {
          "fill-color": "#10B981",
          "fill-opacity": 0.28,
        },
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
        paint: {
          "fill-color": "#F59E0B",
          "fill-opacity": 0.1,
        },
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
      {
        id: "strip-route-casing",
        type: "line",
        source: "strip-route",
        layout: {
          "line-cap": "round",
          "line-join": "round",
        },
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
        layout: {
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#EC4899",
          "line-width": 3.0,
          "line-opacity": 0.96,
        },
      },
      {
        id: "waypoints-legs-casing",
        type: "line",
        source: "waypoints-legs",
        layout: {
          "line-cap": "round",
          "line-join": "round",
        },
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

function renderSheetBadgeImage(label: string): {
  width: number;
  height: number;
  data: Uint8Array;
} | null {
  const scale = 2;
  const logicalW = Math.max(36, 14 + label.length * 8);
  const w = logicalW * scale;
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
  ctx.fillStyle = "rgba(15, 20, 25, 0.88)";
  ctx.fill();
  ctx.lineWidth = 1.5 * scale;
  ctx.strokeStyle = "#F59E0B";
  ctx.stroke();

  ctx.fillStyle = "#FBBF24";
  ctx.font = `700 ${11.5 * scale}px "JetBrains Mono", "SF Mono", ui-monospace, monospace`;
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
  const logicalW = Math.max(42, 16 + extraLeft + code.length * 7.5);
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
      geometry: {
        type: "Point",
        coordinates: centroid,
      },
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
  private pendingOfflineBasemap: BasemapInfoResponse | null = null;
  private offlineBasemapReady = false;
  private reportedExternalTileFail = false;
  private currentFootprint: FeatureCollection | null = null;
  private currentBasemap: BasemapId = "opentopo";
  private suppressClickUntil = 0;
  private draggingMarker = false;
  private markupMode = false;
  private viewshedMode = false;

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
      if (this.viewshedMode) {
        callbacks.onMapViewshedClick?.(lat, lng);
        return;
      }
      if (this.markupMode) {
        callbacks.onMapMarkupClick?.(lat, lng);
        return;
      }
      this.marker.setLngLat([lng, lat]);
      callbacks.onCenterCommit(lat, lng);
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
    });
  }

  enableOfflineBasemap(info: BasemapInfoResponse): void {
    if (!info.available || !info.format) return;
    if (!this.isLoaded) {
      this.pendingOfflineBasemap = info;
      return;
    }
    this.attachOfflineBasemapLayer(info);
  }

  private attachOfflineBasemapLayer(info: BasemapInfoResponse): void {
    if (!info.available || !info.format) return;
    if (this.map.getSource("offline-mbtiles")) {
      this.offlineBasemapReady = true;
      return;
    }

    const ext = info.format === "jpg" ? "jpg" : "png";
    const sourceSpec: maplibregl.RasterSourceSpecification = {
      type: "raster",
      tiles: [`/api/basemap/{z}/{x}/{y}.${ext}`],
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
        paint: {
          "raster-opacity": 1,
        },
      },
      "viewshed-coverage-fill"
    );
    this.offlineBasemapReady = true;
    this.applyBasemapVisibility();
  }

  setMarkupMode(active: boolean): void {
    this.markupMode = active;
    this.container.classList.toggle("markup-active", active);
  }

  setViewshedMode(active: boolean): void {
    this.viewshedMode = active;
    this.container.classList.toggle("los-active", active);
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
    // Slight padding around current sheet for a practical mini pack
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
        geometry: {
          type: "Point",
          coordinates: [wp.lon, wp.lat],
        },
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
                geometry: {
                  type: "LineString",
                  coordinates: legCoords,
                },
              },
            ]
          : [],
    };

    const pointsFc: FeatureCollection<Point> = {
      type: "FeatureCollection",
      features: pointFeatures,
    };

    const legsSrc = this.map.getSource("waypoints-legs") as
      | maplibregl.GeoJSONSource
      | undefined;
    const ptsSrc = this.map.getSource("waypoints-points") as
      | maplibregl.GeoJSONSource
      | undefined;

    legsSrc?.setData(legsFc);
    ptsSrc?.setData(pointsFc);
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

    const covSrc = this.map.getSource("viewshed-coverage") as
      | maplibregl.GeoJSONSource
      | undefined;
    const ringSrc = this.map.getSource("viewshed-rings") as
      | maplibregl.GeoJSONSource
      | undefined;
    const obsSrc = this.map.getSource("viewshed-observers") as
      | maplibregl.GeoJSONSource
      | undefined;

    covSrc?.setData({ type: "FeatureCollection", features: coverageFeatures });
    ringSrc?.setData({ type: "FeatureCollection", features: ringFeatures });
    obsSrc?.setData({ type: "FeatureCollection", features: observerFeatures });
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
