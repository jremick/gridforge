import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection, Position } from "geojson";
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
        attribution: "Map data: © OpenStreetMap contributors, SRTM | Style: © OpenTopoMap (CC-BY-SA)",
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

  constructor(container: HTMLElement, callbacks: GridForgeMapCallbacks) {
    this.map = new maplibregl.Map({
      container,
      style: buildInitialStyle(),
      center: [DEFAULT_CENTER_LON, DEFAULT_CENTER_LAT],
      zoom: DEFAULT_MAP_ZOOM,
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
      .setLngLat([DEFAULT_CENTER_LON, DEFAULT_CENTER_LAT])
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

  isDragging(): boolean {
    return this.draggingMarker;
  }

  setCenter(lat: number, lon: number, options?: { fly?: boolean; zoom?: number }): void {
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

    fpSource?.setData(footprint);
    gridSource?.setData(grid);
  }

  fitFootprint(sidebarWidthPx = 360): void {
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
        bottom: 72,
        left: Math.min(sidebarWidthPx + 56, Math.floor(window.innerWidth * 0.42)),
        right: 84,
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
