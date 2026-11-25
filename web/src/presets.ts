import type {
  AUOverlayKind,
  AppFormState,
  AtlasGridOption,
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
    regionTag: "NSW · 56H",
    lat: -33.732,
    lon: 150.312,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "BLUE MOUNTAINS — KATOOMBA",
    zoom: 12,
  },
  {
    id: "mt-kosciuszko",
    label: "Mt Kosciuszko NSW",
    regionTag: "NSW · 55H",
    lat: -36.456,
    lon: 148.263,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "MT KOSCIUSZKO — MAIN RANGE",
    zoom: 12,
  },
  {
    id: "budawangs",
    label: "Budawangs NSW",
    regionTag: "NSW · 56H",
    lat: -35.276,
    lon: 150.193,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "BUDAWANGS — THE CASTLE",
    zoom: 12,
  },
  {
    id: "warrumbungles",
    label: "Warrumbungles NSW",
    regionTag: "NSW · 55J",
    lat: -31.298,
    lon: 149.005,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "WARRUMBUNGLES — GRAND HIGH TOPS",
    zoom: 12,
  },
  {
    id: "grampians",
    label: "Grampians VIC",
    regionTag: "VIC · 54H",
    lat: -37.145,
    lon: 142.515,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "GRAMPIANS — HALLS GAP",
    zoom: 12,
  },
  {
    id: "wilsons-prom",
    label: "Wilsons Prom VIC",
    regionTag: "VIC · 55H",
    lat: -39.032,
    lon: 146.365,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "WILSONS PROMONTORY — TIDAL RIVER",
    zoom: 12,
  },
  {
    id: "mt-bogong",
    label: "Mt Bogong VIC",
    regionTag: "VIC · 55H",
    lat: -36.733,
    lon: 147.306,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "MT BOGONG — ALPINE NP",
    zoom: 12,
  },
  {
    id: "cathedral-range",
    label: "Cathedral Range VIC",
    regionTag: "VIC · 55H",
    lat: -37.392,
    lon: 145.752,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "CATHEDRAL RANGE — SUGARLOAF",
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

export const ATLAS_GRID_OPTIONS: readonly AtlasGridOption[] = [
  { id: "1x1", label: "1×1", cols: 1, rows: 1 },
  { id: "2x1", label: "2×1", cols: 2, rows: 1 },
  { id: "1x2", label: "1×2", cols: 1, rows: 2 },
  { id: "2x2", label: "2×2", cols: 2, rows: 2 },
  { id: "3x2", label: "3×2", cols: 3, rows: 2 },
  { id: "3x3", label: "3×3", cols: 3, rows: 3 },
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
    grid_cols: 1,
    grid_rows: 1,
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

const LAT_LON_RE =
  /^\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*$/;
const LAT_LON_SPACE_RE =
  /^\s*([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)\s*$/;
const MGRS_RE =
  /^\s*0?(?:[1-9]|[1-5]\d|60)\s*[C-HJ-NP-X]\s*[A-HJ-NP-Z]{2}(?:\s*\d{2,10})?\s*$/i;

export function parseLatLon(
  value: string
): { lat: number; lon: number } | null {
  const m = LAT_LON_RE.exec(value) ?? LAT_LON_SPACE_RE.exec(value);
  if (!m) return null;
  const lat = Number(m[1]);
  const lon = Number(m[2]);
  if (
    !Number.isFinite(lat) ||
    !Number.isFinite(lon) ||
    lat < -80 ||
    lat > 84 ||
    lon < -180 ||
    lon > 180
  ) {
    return null;
  }
  return { lat, lon };
}

export function looksLikeCoordOrMgrs(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (parseLatLon(trimmed) !== null) return true;
  return MGRS_RE.test(trimmed);
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

export function formatSignedMilsNumber(mils: number): string {
  const rounded = Math.round(mils);
  if (rounded === 0) return "0";
  return `${rounded > 0 ? "+" : "−"}${Math.abs(rounded)}`;
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
  merging_atlas: "Merging multi-sheet Atlas PDF",
  exporting_mbtiles: "Exporting ATAK .mbtiles",
  exporting_kmz: "Exporting Garmin .kmz",
  finalizing: "Finalizing artifacts",
  done: "Completed",
  completed: "Completed",
  failed: "Render failed",
  cancelled: "Cancelled",
};

export function formatStage(stage: string): string {
  const trimmed = stage.trim();
  const key = trimmed.toLowerCase();
  if (STAGE_LABELS[key]) return STAGE_LABELS[key];
  const sheetMatch = /^rendering_sheet_([A-Z]\d+)(.*)$/i.exec(trimmed);
  if (sheetMatch) {
    return `Rendering sheet ${sheetMatch[1]?.toUpperCase() ?? ""}${sheetMatch[2] ?? ""}`;
  }
  return trimmed.replace(/_/g, " ");
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

function shellQuote(arg: string): string {
  if (/^[a-zA-Z0-9._/:=-]+$/.test(arg)) return arg;
  return `"${arg.replace(/["\\$`]/g, "\\$&")}"`;
}

function slugifyFilename(state: AppFormState): string {
  const raw = state.title.trim() || `sheet_${Math.round(state.scale / 1000)}k`;
  const slug = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  return `${slug || "gridforge_sheet"}_${state.paper}_${state.color_mode}`;
}

export function buildCliCommand(state: AppFormState): string {
  const centerArg = state.center.trim() || formatCoords(state.lat, state.lon);
  const slug = slugifyFilename(state);
  const pdfEnabled = state.outputs.pdf;
  const subcmd = pdfEnabled ? "render" : "export";

  const args: string[] = [
    "docker compose run --rm cli gridforge",
    subcmd,
    `--center ${shellQuote(centerArg)}`,
    `--scale ${state.scale}`,
    `--paper ${state.paper}`,
    `--orientation ${state.orientation}`,
    `--mode ${state.color_mode}`,
    `--units ${state.units}`,
  ];

  if (state.title.trim()) {
    args.push(`--title ${shellQuote(state.title.trim())}`);
  }
  if (state.dem_source !== "auto") {
    args.push(`--dem-source ${state.dem_source}`);
  }
  if (state.pack_id) {
    args.push(`--pack ${shellQuote(`/data/packs/${state.pack_id}`)}`);
  }
  for (const ov of state.au_overlays) {
    args.push(`--au-overlay ${ov}`);
  }
  for (const ov of state.us_overlays) {
    args.push(`--us-overlay ${ov}`);
  }

  if (state.carto.contour_density !== "normal") {
    args.push(`--contour-density ${state.carto.contour_density}`);
  }
  if (state.carto.hillshade_strength !== "normal") {
    args.push(`--hillshade-strength ${state.carto.hillshade_strength}`);
  }
  if (state.carto.label_density !== "normal") {
    args.push(`--label-density ${state.carto.label_density}`);
  }
  if (!state.carto.show_buildings) args.push("--no-buildings");
  if (!state.carto.show_trees) args.push("--no-trees");
  if (!state.carto.show_power) args.push("--no-power");
  if (!state.carto.show_pipelines) args.push("--no-pipelines");
  if (!state.carto.show_river_arrows) args.push("--no-river-arrows");
  if (pdfEnabled && !state.carto.show_grid) args.push("--no-grid");
  if (!pdfEnabled && !state.outputs.include_grid_on_digital) {
    args.push("--no-grid");
  }

  if (state.overlayFile) {
    args.push(`--overlay ${shellQuote(`/output/${state.overlayFile.name}`)}`);
  }

  if (pdfEnabled) {
    if (state.outputs.geopdf) args.push("--geopdf");
    if (state.outputs.mbtiles) {
      args.push(`--mbtiles /output/${slug}.mbtiles`);
      if (state.outputs.mbtiles_format !== "png") {
        args.push(`--mbtiles-format ${state.outputs.mbtiles_format}`);
      }
    }
    if (state.outputs.kmz) {
      args.push(`--kmz /output/${slug}.kmz`);
      if (state.outputs.garmin_profile !== "standard") {
        args.push(`--garmin-profile ${state.outputs.garmin_profile}`);
      }
    }
    args.push(`--output /output/${slug}.pdf`);
  } else {
    const fmt =
      state.outputs.mbtiles && state.outputs.kmz
        ? "both"
        : state.outputs.mbtiles
          ? "mbtiles"
          : "kmz";
    args.push(`--format ${fmt}`);
    if (state.outputs.mbtiles && state.outputs.mbtiles_format !== "png") {
      args.push(`--mbtiles-format ${state.outputs.mbtiles_format}`);
    }
    if (state.outputs.kmz && state.outputs.garmin_profile !== "standard") {
      args.push(`--garmin-profile ${state.outputs.garmin_profile}`);
    }
    const ext = fmt === "both" ? "" : `.${fmt}`;
    args.push(`--output /output/${slug}${ext}`);
  }

  return args.join(" ");
}

export function escapeHtml(input: string): string {
  return input
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
