import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Feature, FeatureCollection, Point, Position } from "geojson";
import {
  DEFAULT_CENTER_LAT,
  DEFAULT_CENTER_LON,
  DEFAULT_MAP_ZOOM,
} from "./presets";
import type { BasemapId } from "./types";

const EMPTY_FC: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

export interface GridForgeMapCallbacks {
  onCenterDrag: (lat: number, lon: number) => void;
  onCenterCommit: (lat: number, lon: number) => void;
  onCursorMove?: (lat: number, lon: number) => void;
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
      "sheet-labels": {
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
        id: "sheet-labels-layer",
        type: "symbol",
        source: "sheet-labels",
        layout: {
          "icon-image": ["get", "icon_id"],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
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
  const w = 36 * scale;
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
  ctx.font = `700 ${12 * scale}px "JetBrains Mono", "SF Mono", ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, w / 2, h / 2 + 0.5 * scale);

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
    const label = rawLabel.trim().toUpperCase().slice(0, 4);
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
  private readonly map: maplibregl.Map;
  private readonly marker: maplibregl.Marker;
  private isLoaded = false;
  private pendingFootprint: FeatureCollection | null = null;
  private pendingGrid: FeatureCollection | null = null;
  private currentFootprint: FeatureCollection | null = null;
  private currentBasemap: BasemapId = "opentopo";
  private suppressClickUntil = 0;
  private draggingMarker = false;

  constructor(
    container: HTMLElement,
    callbacks: GridForgeMapCallbacks,
    options?: GridForgeMapOptions
  ) {
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
      this.marker.setLngLat([lng, lat]);
      callbacks.onCenterCommit(lat, lng);
    });

    this.map.on("mousemove", (e) => {
      callbacks.onCursorMove?.(e.lngLat.lat, e.lngLat.lng);
    });

    this.map.on("load", () => {
      this.isLoaded = true;
      this.applyBasemapVisibility();
      if (this.pendingFootprint || this.pendingGrid) {
        this.applyGeoJson(
          this.pendingFootprint ?? EMPTY_FC,
          this.pendingGrid ?? EMPTY_FC
        );
        this.pendingFootprint = null;
        this.pendingGrid = null;
      }
    });
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

  clearFootprint(): void {
    this.updateFootprint(EMPTY_FC, EMPTY_FC);
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
