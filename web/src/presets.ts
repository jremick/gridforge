import type {
  AUOverlayKind,
  AppFormState,
  BasemapId,
  DemSourceKind,
  DensityLevel,
  HillshadeStrength,
  LocationPreset,
  PaperSize,
  USOverlayKind,
} from "./types";

export const DEFAULT_CENTER_LAT = -33.732;
export const DEFAULT_CENTER_LON = 150.312;
export const DEFAULT_MAP_ZOOM = 12;

export const LOCATION_PRESETS: readonly LocationPreset[] = [
  {
    id: "blue-mountains",
    label: "Blue Mountains NSW",
    regionTag: "AU · 56H",
    lat: -33.732,
    lon: 150.312,
    scale: 25000,
    paper: "a4",
    units: "metric",
    defaultTitle: "BLUE MOUNTAINS NSW",
    zoom: 12,
  },
  {
    id: "sydney-harbour",
    label: "Sydney Harbour NSW",
    regionTag: "AU · 56H",
    lat: -33.8568,
    lon: 151.2153,
    scale: 25000,
    paper: "a4",
    units: "metric",
    defaultTitle: "SYDNEY HARBOUR NSW",
    zoom: 12.5,
  },
  {
    id: "australian-alps",
    label: "Australian Alps (Kosciuszko)",
    regionTag: "AU · 55H",
    lat: -36.456,
    lon: 148.263,
    scale: 25000,
    paper: "a4",
    units: "metric",
    defaultTitle: "MT KOSCIUSZKO NSW",
    zoom: 12,
  },
  {
    id: "cradle-mountain",
    label: "Cradle Mountain TAS",
    regionTag: "AU · 55G",
    lat: -41.684,
    lon: 145.951,
    scale: 25000,
    paper: "a4",
    units: "metric",
    defaultTitle: "CRADLE MOUNTAIN TAS",
    zoom: 12,
  },
  {
    id: "uluru",
    label: "Uluru / Kata Tjuta NT",
    regionTag: "AU · 52J",
    lat: -25.3444,
    lon: 131.0369,
    scale: 25000,
    paper: "a4",
    units: "metric",
    defaultTitle: "ULURU — KATA TJUTA NT",
    zoom: 12,
  },
  {
    id: "perth-hills",
    label: "Perth Hills WA",
    regionTag: "AU · 50H",
    lat: -31.95,
    lon: 116.06,
    scale: 25000,
    paper: "a4",
    units: "metric",
    defaultTitle: "PERTH HILLS WA",
    zoom: 12,
  },
  {
    id: "daintree",
    label: "Daintree / Cairns QLD",
    regionTag: "AU · 55K",
    lat: -16.25,
    lon: 145.418,
    scale: 25000,
    paper: "a4",
    units: "metric",
    defaultTitle: "DAINTREE QLD",
    zoom: 12,
  },
  {
    id: "boulder",
    label: "Boulder / Flatirons CO",
    regionTag: "US · 13T",
    lat: 40.005,
    lon: -105.285,
    scale: 24000,
    paper: "letter",
    units: "imperial",
    defaultTitle: "BOULDER FLATIRONS CO",
    zoom: 12,
  },
];

export const SCALE_OPTIONS: readonly { value: number; label: string }[] = [
  { value: 15000, label: "1:15,000" },
  { value: 24000, label: "1:24,000" },
  { value: 25000, label: "1:25,000" },
  { value: 48000, label: "1:48,000" },
  { value: 50000, label: "1:50,000" },
];

export const PAPER_OPTIONS: readonly {
  value: PaperSize;
  label: string;
  dims: string;
}[] = [
  { value: "a4", label: "A4", dims: "210 × 297 mm" },
  { value: "letter", label: "Letter", dims: "8.5 × 11 in" },
  { value: "legal", label: "Legal", dims: "8.5 × 14 in" },
  { value: "a3", label: "A3", dims: "297 × 420 mm" },
];

export const DEM_SOURCE_OPTIONS: readonly {
  value: DemSourceKind;
  label: string;
}[] = [
  { value: "auto", label: "Auto (Best Regional Elevation)" },
  { value: "au-elvis", label: "Australia GA 5m / SRTM (AHD)" },
  { value: "glo30", label: "Copernicus GLO-30 (EGM2008)" },
  { value: "usgs3dep", label: "USGS 3DEP 1m / 10m (NAVD88)" },
];

export const AU_OVERLAY_OPTIONS: readonly {
  value: AUOverlayKind;
  label: string;
  hint: string;
}[] = [
  {
    value: "au-hydro",
    label: "GA Hydrology (Bores/Streams)",
    hint: "Watercourses, bores & waterholes",
  },
  {
    value: "au-parks",
    label: "CAPAD National Parks & IPAs",
    hint: "Protected area boundaries",
  },
  {
    value: "au-infra",
    label: "GA Power & Pipelines",
    hint: "HV lines, substations & pipelines",
  },
  {
    value: "au-aviation",
    label: "GA Airfields & Homesteads",
    hint: "Regional strips, runways & stations",
  },
];

export const US_OVERLAY_OPTIONS: readonly {
  value: USOverlayKind;
  label: string;
  hint: string;
}[] = [
  {
    value: "fema",
    label: "FEMA Flood Zones",
    hint: "NFHL 100-yr / 500-yr floodplains",
  },
  {
    value: "nhd",
    label: "USGS NHD Streams",
    hint: "High-res national hydrography",
  },
  {
    value: "towers",
    label: "FAA Obstacle Towers",
    hint: "DOF towers, masts & AGL heights",
  },
  {
    value: "enc",
    label: "NOAA ENC Charts",
    hint: "Coastal depth soundings & buoys",
  },
];

export const DENSITY_OPTIONS: readonly { value: DensityLevel; label: string }[] =
  [
    { value: "low", label: "Low" },
    { value: "normal", label: "Normal" },
    { value: "high", label: "High" },
  ];

export const HILLSHADE_OPTIONS: readonly {
  value: HillshadeStrength;
  label: string;
}[] = [
  { value: "off", label: "Off" },
  { value: "light", label: "Light" },
  { value: "normal", label: "Normal" },
  { value: "strong", label: "Strong" },
];

export const BASEMAP_OPTIONS: readonly { id: BasemapId; label: string }[] = [
  { id: "opentopo", label: "OpenTopoMap" },
  { id: "osm", label: "OSM Standard" },
  { id: "satellite", label: "Esri Imagery" },
];

export const CARTO_TOGGLES: readonly {
  key:
    | "show_buildings"
    | "show_trees"
    | "show_power"
    | "show_pipelines"
    | "show_grid"
    | "show_river_arrows";
  label: string;
}[] = [
  { key: "show_buildings", label: "Buildings" },
  { key: "show_trees", label: "Trees / Woodland" },
  { key: "show_power", label: "Power Lines" },
  { key: "show_pipelines", label: "Pipelines" },
  { key: "show_grid", label: "MGRS Grid" },
  { key: "show_river_arrows", label: "River Flow Arrows" },
];

export function createInitialFormState(): AppFormState {
  return {
    center: `${DEFAULT_CENTER_LAT}, ${DEFAULT_CENTER_LON}`,
    lat: DEFAULT_CENTER_LAT,
    lon: DEFAULT_CENTER_LON,
    title: "",
    scale: 25000,
    paper: "a4",
    orientation: "portrait",
    color_mode: "color",
    units: "metric",
    dem_source: "auto",
    pack_id: "",
    au_overlays: [],
    us_overlays: [],
    carto: {
      contour_density: "normal",
      hillshade_strength: "normal",
      label_density: "normal",
      show_buildings: true,
      show_trees: true,
      show_power: true,
      show_pipelines: true,
      show_grid: true,
      show_river_arrows: true,
    },
    outputs: {
      pdf: true,
      geopdf: false,
      mbtiles: false,
      mbtiles_format: "png",
      kmz: false,
      garmin_profile: "standard",
      include_grid_on_digital: true,
    },
    overlayFile: null,
  };
}

export function formatCoords(lat: number, lon: number): string {
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

export function formatSignedDeg(deg: number): string {
  const abs = Math.abs(deg).toFixed(1);
  if (Math.abs(deg) < 0.05) return "0.0°";
  return `${deg > 0 ? "+" : "−"}${abs}°`;
}

export function formatDeclinationCompass(deg: number): string {
  const abs = Math.abs(deg).toFixed(1);
  if (Math.abs(deg) < 0.05) return "0.0°";
  return `${abs}° ${deg > 0 ? "E" : "W"}`;
}

export function formatMils(mils: number): string {
  const rounded = Math.round(mils);
  if (rounded === 0) return "0 mils";
  return `${rounded > 0 ? "+" : "−"}${Math.abs(rounded)} mils`;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const decimals = unitIndex === 0 ? 0 : value >= 100 ? 0 : 1;
  return `${value.toFixed(decimals)} ${units[unitIndex] ?? "B"}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const mins = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return `${mins}m ${rem.toString().padStart(2, "0")}s`;
}

const STAGE_LABELS: Record<string, string> = {
  queued: "Queued for worker",
  preparing: "Preparing sheet geometry",
  fetching_data: "Fetching DEM & OSM data",
  fetching_dem: "Streaming elevation DEM",
  fetching_osm: "Fetching OSM & overlays",
  terrain: "Generating contours & hillshade",
  building_terrain: "Generating contours & hillshade",
  rendering: "Rendering PyQGIS map sheet",
  rendering_pdf: "Rendering 300 DPI print PDF",
  exporting_mbtiles: "Exporting ATAK .mbtiles",
  exporting_kmz: "Exporting Garmin .kmz",
  finalizing: "Finalizing artifacts",
  done: "Completed",
  completed: "Completed",
  failed: "Render failed",
  cancelled: "Cancelled",
};

export function formatStage(stage: string): string {
  const key = stage.trim().toLowerCase();
  return STAGE_LABELS[key] ?? stage.replace(/_/g, " ");
}

export function formatArtifactLabel(kind: string, filename: string): string {
  const lowerKind = kind.toLowerCase();
  const lowerName = filename.toLowerCase();
  if (lowerKind.includes("geopdf")) return "GeoPDF";
  if (lowerKind.includes("pdf") || lowerName.endsWith(".pdf")) return "Print PDF";
  if (lowerKind.includes("mbtiles") || lowerName.endsWith(".mbtiles")) {
    return "ATAK .mbtiles";
  }
  if (lowerKind.includes("kmz") || lowerName.endsWith(".kmz")) {
    return "Garmin .kmz";
  }
  return filename;
}

export function escapeHtml(input: string): string {
  return input
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
