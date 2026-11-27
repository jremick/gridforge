import type { FeatureCollection } from "geojson";

export type PaperSize = "a4" | "letter" | "legal" | "a3";
export type Orientation = "portrait" | "landscape";
export type ColorMode = "color" | "bw" | "night-red" | "alpine";
export type Units = "metric" | "imperial";
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type RFLinkStatus = "CLEAR" | "MARGINAL" | "OBSTRUCTED";
export type WaypointCategory = "waypoint" | "camp" | "water" | "hazard" | "lz";
export type PackRegionGroup = "au_tactical" | "au_state" | "nz" | "na";

export interface BBox {
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
}

export interface StudioPreset {
  id: string;
  name: string;
  region: string;
  center: string;
  scale: number;
  paper: PaperSize;
  color_mode: ColorMode;
  orientation?: Orientation;
}

export interface PackSummary {
  pack_id: string;
  name: string;
  region: string;
  bbox_wgs84?: BBox | null;
  utm_zones?: string[];
  vertical_datum?: string;
  size_bytes?: number;
  has_dem?: boolean;
  has_osm?: boolean;
  has_basemap?: boolean;
  overlays?: string[];
}

export interface PackRegionSummary {
  region_id: string;
  name: string;
  group: PackRegionGroup | string;
  kind?: "state" | "tactical" | string;
  bbox_wgs84?: BBox | null;
  utm_zones?: string[];
  description?: string;
  estimated_size_hint?: string;
}

export interface DeclinationData {
  model_name: string;
  decimal_year: number;
  magnetic_declination_deg: number;
  annual_change_deg: number;
  grid_convergence_deg: number;
  gm_angle_deg: number;
  gm_angle_mils: number;
}

export interface BriefingLocation {
  center: string;
  lat: number;
  lon: number;
  mgrs_10m: string;
  utm_epsg: number;
  utm_zone: number;
  utm_hemisphere: "N" | "S";
  gzd: string;
  square_ids_100km: string[];
  place_name?: string | null;
}

export interface BriefingSheet {
  scale: number;
  paper: PaperSize;
  orientation: Orientation;
  color_mode: ColorMode;
  units: Units;
  page_width_mm: number;
  page_height_mm: number;
  ground_width_km: number;
  ground_height_km: number;
  total_ground_width_km?: number;
  total_ground_height_km?: number;
  grid_cols: number;
  grid_rows: number;
  sheet_count: number;
  grid_spacing_mm: number;
  dpi?: number;
}

export interface BriefingRecommendations {
  dem_source: string;
  region_label: string;
  overlays: string[];
  matching_packs: string[];
}

export interface WaypointItem {
  label: string;
  lat: number;
  lon: number;
  category?: WaypointCategory | string;
  notes?: string | null;
  seq?: number;
  code?: string;
  mgrs_10m?: string;
  elev_m?: number | null;
}

export interface RFLinkRequestData {
  from_lat: number;
  from_lon: number;
  to_lat: number;
  to_lon: number;
  freq_mhz: number;
  from_height_m: number;
  to_height_m: number;
  from_label: string;
  to_label: string;
  dem_source?: string | null;
  pack_id?: string | null;
}

export interface RFLinkSample {
  dist_km: number;
  terrain_m: number;
  los_m: number;
  fresnel_lo_m: number;
  fresnel_hi_m: number;
  fresnel60_lo_m: number;
  clearance_m: number;
}

export interface RFLinkProfileData {
  status: RFLinkStatus;
  distance_km: number;
  freq_mhz: number;
  fspl_db: number;
  min_clearance_m: number;
  min_fresnel_pct: number;
  los_clear: boolean;
  fresnel_60pct_clear: boolean;
  bearing_true_deg: number;
  bearing_grid_deg: number;
  bearing_mag_deg: number;
  bearing_mils: number;
  from_elev_m: number | null;
  to_elev_m: number | null;
  from_mgrs: string;
  to_mgrs: string;
  from_label: string;
  to_label: string;
  from_height_m: number;
  to_height_m: number;
  samples: RFLinkSample[];
}

export interface ViewshedObserverData {
  label: string;
  lat: number;
  lon: number;
  observer_height_m: number;
  target_height_m: number;
  max_distance_m: number;
}

export interface ViewshedResultData {
  observer_mgrs_10m: string;
  observer_elev_m: number | null;
  utm_epsg: number;
  max_distance_m: number;
  visible_area_km2: number;
  viewshed_geojson: FeatureCollection;
  range_ring_geojson: FeatureCollection;
}

export interface RouteLeg {
  from_code: string;
  to_code: string;
  leg_dist_km: number;
  leg_dist_mi?: number;
  cum_dist_km: number;
  true_az_deg: number;
  grid_az_deg: number;
  mag_az_deg: number;
  mag_az_mils: number;
}

export interface RouteMetricsData {
  waypoints: WaypointItem[];
  legs: RouteLeg[];
  total_distance_km: number;
  total_distance_mi?: number;
}

export interface RulerSegmentData {
  from_idx: number;
  to_idx: number;
  distance_m: number;
  bearing_true_deg: number;
  bearing_grid_deg: number;
  bearing_mag_deg: number;
  bearing_mils: number;
  elev_start_m?: number | null;
  elev_end_m?: number | null;
  grade_pct?: number | null;
}

export interface RulerMetricsData {
  total_distance_m: number;
  segments: RulerSegmentData[];
}

export interface StripSheetData {
  sheet_id: string;
  index: number;
  center_lat: number;
  center_lon: number;
  center_mgrs_10m?: string;
  utm_bbox?: BBox;
}

export interface StripPlanData {
  utm_epsg?: number;
  utm_zone?: number;
  utm_hemisphere?: "N" | "S";
  gzd?: string;
  route_distance_km: number;
  sheet_count?: number;
  sheets: StripSheetData[];
  route_geojson?: FeatureCollection;
  footprint_geojson?: FeatureCollection;
  grid_geojson?: FeatureCollection;
}

export interface JobArtifactData {
  kind: string;
  filename: string;
  size_bytes: number;
  download_url: string;
  signed_download_url?: string | null;
}

export interface JobRecordData {
  job_id: string;
  status: JobStatus;
  stage: string;
  progress_pct: number;
  title?: string | null;
  center: string;
  center_mgrs?: string | null;
  scale: number;
  paper: PaperSize;
  orientation: Orientation;
  color_mode: ColorMode;
  dpi?: number;
  grid_cols?: number;
  grid_rows?: number;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  elapsed_seconds?: number | null;
  peak_rss_mb?: number | null;
  error?: string | null;
  artifacts?: JobArtifactData[];
}

export interface JobPageInfo {
  page: number;
  preview_url: string;
  signed_preview_url?: string | null;
}

export interface JobPagePayload {
  job_id: string;
  page: number;
  page_count: number;
  mime_type: string;
  data_base64: string;
}

export interface GeocodeResultItem {
  display_name: string;
  short_name: string;
  lat: number;
  lon: number;
  type: string;
}

export interface StudioViewPayload {
  view: "studio";
  title?: string;
  summary?: string;
  presets?: StudioPreset[];
  capabilities?: Record<string, unknown>;
  packs?: PackSummary[];
  public_url?: string | null;
  share_url?: string | null;
}

export interface BriefingViewPayload {
  view: "briefing";
  title?: string;
  summary?: string;
  question?: string | null;
  answer?: string | null;
  location: BriefingLocation;
  sheet: BriefingSheet;
  declination: DeclinationData;
  recommendations?: BriefingRecommendations | null;
  footprint_geojson?: FeatureCollection | null;
  grid_geojson?: FeatureCollection | null;
  waypoints?: WaypointItem[];
  share_url?: string | null;
  studio_url?: string | null;
}

export interface RFLinkViewPayload {
  view: "rf_link";
  title?: string;
  summary?: string;
  answer?: string | null;
  request: RFLinkRequestData;
  profile: RFLinkProfileData;
  share_url?: string | null;
  studio_url?: string | null;
}

export interface ViewshedViewPayload {
  view: "viewshed";
  title?: string;
  summary?: string;
  answer?: string | null;
  observer: ViewshedObserverData;
  result: ViewshedResultData;
  share_url?: string | null;
  studio_url?: string | null;
}

export interface RouteViewPayload {
  view: "route";
  title?: string;
  summary?: string;
  answer?: string | null;
  waypoints?: WaypointItem[];
  route_metrics?: RouteMetricsData | null;
  ruler_metrics?: RulerMetricsData | null;
  strip_plan?: StripPlanData | null;
  footprint_geojson?: FeatureCollection | null;
  grid_geojson?: FeatureCollection | null;
  share_url?: string | null;
  studio_url?: string | null;
}

export interface RenderViewPayload {
  view: "render";
  title?: string;
  summary?: string;
  answer?: string | null;
  job: JobRecordData;
  page_count: number;
  pages?: JobPageInfo[];
  share_url?: string | null;
  studio_url?: string | null;
}

export interface GeocodeViewPayload {
  view: "geocode";
  title?: string;
  summary?: string;
  query?: string;
  results: GeocodeResultItem[];
}

export interface PacksViewPayload {
  view: "packs";
  title?: string;
  summary?: string;
  packs: PackSummary[];
  regions?: PackRegionSummary[];
}

export type GridForgeViewPayload =
  | StudioViewPayload
  | BriefingViewPayload
  | RFLinkViewPayload
  | ViewshedViewPayload
  | RouteViewPayload
  | RenderViewPayload
  | GeocodeViewPayload
  | PacksViewPayload;

export type KnownViewName = GridForgeViewPayload["view"];

export interface HostCapabilityFlags {
  serverTools: boolean;
  openLinks: boolean;
  message: boolean;
  updateModelContext: boolean;
}

export const VALID_SCALES = [10000, 12500, 20000, 24000, 25000, 50000] as const;

export const RF_BAND_PRESETS: ReadonlyArray<{
  label: string;
  shortLabel: string;
  freq_mhz: number;
}> = [
  { label: "915 MHz LoRa", shortLabel: "915 LoRa", freq_mhz: 915 },
  { label: "868 MHz LoRa", shortLabel: "868 LoRa", freq_mhz: 868 },
  { label: "477 MHz UHF CB", shortLabel: "477 UHF CB", freq_mhz: 477 },
  { label: "462 MHz GMRS", shortLabel: "462 GMRS", freq_mhz: 462 },
  { label: "146 MHz VHF", shortLabel: "146 VHF", freq_mhz: 146 },
  { label: "2.4 GHz ISM", shortLabel: "2.4 GHz", freq_mhz: 2400 },
];

export const ATLAS_PRESETS: ReadonlyArray<{
  id: string;
  label: string;
  cols: number;
  rows: number;
}> = [
  { id: "1x1", label: "1×1 Single", cols: 1, rows: 1 },
  { id: "2x1", label: "2×1 Wide", cols: 2, rows: 1 },
  { id: "1x2", label: "1×2 Tall", cols: 1, rows: 2 },
  { id: "2x2", label: "2×2 Quad", cols: 2, rows: 2 },
  { id: "3x2", label: "3×2 Atlas", cols: 3, rows: 2 },
  { id: "2x3", label: "2×3 Atlas", cols: 2, rows: 3 },
  { id: "3x3", label: "3×3 Max", cols: 3, rows: 3 },
];

export const DEFAULT_STUDIO_PRESETS: StudioPreset[] = [
  {
    id: "blue-mountains",
    name: "Blue Mountains (Katoomba)",
    region: "AU · NSW",
    center: "-33.7152, 150.3119",
    scale: 25000,
    paper: "a4",
    color_mode: "color",
  },
  {
    id: "kosciuszko",
    name: "Mt Kosciuszko Alpine",
    region: "AU · NSW",
    center: "-36.4559, 148.2636",
    scale: 25000,
    paper: "a4",
    color_mode: "alpine",
  },
  {
    id: "yosemite",
    name: "Yosemite Valley",
    region: "US · CA",
    center: "37.7456, -119.5936",
    scale: 24000,
    paper: "letter",
    color_mode: "alpine",
  },
  {
    id: "banff",
    name: "Banff & Lake Louise",
    region: "CA · AB",
    center: "51.4254, -116.1773",
    scale: 25000,
    paper: "letter",
    color_mode: "alpine",
  },
  {
    id: "tongariro",
    name: "Tongariro Alpine Crossing",
    region: "NZ · North Island",
    center: "-39.1328, 175.6422",
    scale: 25000,
    paper: "a4",
    color_mode: "color",
  },
];

export function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function formatBytes(bytes: number | undefined | null): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function formatSignedDeg(deg: number, digits = 1): string {
  if (!Number.isFinite(deg)) return "0.0°";
  const sign = deg > 0.005 ? "+" : deg < -0.005 ? "−" : "";
  const suffix = deg > 0.005 ? " E" : deg < -0.005 ? " W" : "";
  return `${sign}${Math.abs(deg).toFixed(digits)}°${suffix}`;
}

export function formatSignedMils(mils: number): string {
  if (!Number.isFinite(mils)) return "0 mils";
  const rounded = Math.round(mils);
  const sign = rounded > 0 ? "+" : rounded < 0 ? "−" : "";
  return `${sign}${Math.abs(rounded)} mils`;
}

export function formatScale(scale: number): string {
  if (!Number.isFinite(scale) || scale <= 0) return "1:25,000";
  return `1:${Math.round(scale).toLocaleString("en-US")}`;
}

export function formatArtifactLabel(kind: string): string {
  switch (kind.toLowerCase()) {
    case "pdf":
      return "Printable PDF";
    case "mbtiles":
      return "ATAK MBTiles";
    case "kmz":
      return "Garmin KMZ";
    case "gpkg":
      return "GeoPackage";
    case "sqlitedb":
      return "OsmAnd SQLite";
    case "zip":
      return "Field Bundle ZIP";
    default:
      return kind.toUpperCase();
  }
}

export function isKnownViewPayload(value: unknown): value is GridForgeViewPayload {
  if (!value || typeof value !== "object") return false;
  const v = (value as { view?: unknown }).view;
  return (
    v === "studio" ||
    v === "briefing" ||
    v === "rf_link" ||
    v === "viewshed" ||
    v === "route" ||
    v === "render" ||
    v === "geocode" ||
    v === "packs"
  );
}

export function mapPackGroupToFilter(group: string): "AU" | "NZ" | "NA" | "OTHER" {
  const g = group.toLowerCase();
  if (g.startsWith("au")) return "AU";
  if (g.startsWith("nz")) return "NZ";
  if (g.startsWith("na") || g.startsWith("us") || g.startsWith("ca")) return "NA";
  return "OTHER";
}

export function isTerminalJobStatus(status: JobStatus | string | undefined): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

export function extractStructuredPayload(
  result: unknown
): GridForgeViewPayload | null {
  if (!result || typeof result !== "object") return null;
  const r = result as {
    structuredContent?: unknown;
    content?: Array<{ type?: string; text?: string }>;
  };
  if (isKnownViewPayload(r.structuredContent)) {
    return r.structuredContent;
  }
  if (isKnownViewPayload(result)) {
    return result;
  }
  if (Array.isArray(r.content)) {
    for (const block of r.content) {
      if (block?.type === "text" && typeof block.text === "string") {
        const trimmed = block.text.trim();
        if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
          try {
            const parsed = JSON.parse(trimmed);
            if (isKnownViewPayload(parsed)) return parsed;
          } catch (err) {
            // Plain markdown prose rather than JSON payload
            console.debug("Skipping non-JSON text block:", err);
          }
        }
      }
    }
  }
  return null;
}

export function buildModelContextSummary(payload: GridForgeViewPayload): string {
  switch (payload.view) {
    case "briefing": {
      const loc = payload.location;
      const sh = payload.sheet;
      const d = payload.declination;
      return [
        `# GridForge Location Briefing: ${loc.place_name || loc.center}`,
        `- Coordinates: ${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)}`,
        `- 10m MGRS: ${loc.mgrs_10m}`,
        `- UTM Zone: ${loc.utm_zone}${loc.utm_hemisphere} (EPSG:${loc.utm_epsg}, GZD ${loc.gzd})`,
        `- Sheet Spec: ${formatScale(sh.scale)}, ${sh.paper.toUpperCase()} ${sh.orientation}, ${sh.color_mode}, ${sh.grid_cols}x${sh.grid_rows} (${sh.ground_width_km.toFixed(2)} x ${sh.ground_height_km.toFixed(2)} km per sheet)`,
        `- WMM2025 Declination (${d.decimal_year.toFixed(1)}): Mag ${formatSignedDeg(d.magnetic_declination_deg)}, Grid Conv ${formatSignedDeg(d.grid_convergence_deg, 2)}, G-M ${formatSignedDeg(d.gm_angle_deg)} (${formatSignedMils(d.gm_angle_mils)})`,
        payload.recommendations
          ? `- Open Data: ${payload.recommendations.dem_source} (${payload.recommendations.region_label}); overlays: ${payload.recommendations.overlays.join(", ") || "none"}`
          : "",
      ]
        .filter(Boolean)
        .join("\n");
    }
    case "rf_link": {
      const p = payload.profile;
      return [
        `# GridForge RF Link Analysis (${p.status})`,
        `- Link: ${p.from_label} (${p.from_mgrs}) -> ${p.to_label} (${p.to_mgrs})`,
        `- Distance: ${p.distance_km.toFixed(2)} km | Freq: ${p.freq_mhz} MHz | FSPL: ${p.fspl_db.toFixed(1)} dB`,
        `- Clearance: min ${p.min_clearance_m.toFixed(1)} m (${p.min_fresnel_pct.toFixed(0)}% F1) | LOS: ${p.los_clear ? "Clear" : "Blocked"} | 60% F1: ${p.fresnel_60pct_clear ? "Clear" : "Margin/Blocked"}`,
        `- Bearing: True ${p.bearing_true_deg.toFixed(1)}° / Grid ${p.bearing_grid_deg.toFixed(1)}° / Mag ${p.bearing_mag_deg.toFixed(1)}° (${Math.round(p.bearing_mils)} mils)`,
      ].join("\n");
    }
    case "viewshed": {
      const o = payload.observer;
      const r = payload.result;
      return [
        `# GridForge Viewshed Coverage: ${o.label}`,
        `- Observer: ${o.lat.toFixed(5)}, ${o.lon.toFixed(5)} (${r.observer_mgrs_10m}, elev ${r.observer_elev_m != null ? Math.round(r.observer_elev_m) + "m" : "N/A"})`,
        `- Antenna/Observer Ht: ${o.observer_height_m}m | Target Ht: ${o.target_height_m}m | Max Radius: ${(r.max_distance_m / 1000).toFixed(1)} km`,
        `- Visible Area: ${r.visible_area_km2.toFixed(2)} km² (EPSG:${r.utm_epsg})`,
      ].join("\n");
    }
    case "route": {
      const distKm =
        payload.strip_plan?.route_distance_km ??
        payload.route_metrics?.total_distance_km ??
        (payload.ruler_metrics ? payload.ruler_metrics.total_distance_m / 1000 : 0);
      const sheets = payload.strip_plan?.sheets?.length ?? 0;
      const legs =
        payload.route_metrics?.legs?.length ??
        payload.ruler_metrics?.segments?.length ??
        0;
      return [
        `# GridForge Route & Strip Plan: ${payload.title || "Tactical Route"}`,
        `- Total Distance: ${distKm.toFixed(2)} km`,
        `- Legs: ${legs}${sheets ? ` | Strip Atlas Sheets: ${sheets}` : ""}`,
      ].join("\n");
    }
    case "render": {
      const j = payload.job;
      return [
        `# GridForge Render Job: ${j.title || j.job_id}`,
        `- Status: ${j.status} (${j.progress_pct}%) | Center: ${j.center}${j.center_mgrs ? ` (${j.center_mgrs})` : ""}`,
        `- Spec: ${formatScale(j.scale)} ${j.paper.toUpperCase()} ${j.orientation} ${j.color_mode} (${payload.page_count} page${payload.page_count === 1 ? "" : "s"})`,
        j.artifacts?.length
          ? `- Artifacts: ${j.artifacts.map((a) => `${a.filename} (${formatBytes(a.size_bytes)})`).join(", ")}`
          : "",
      ]
        .filter(Boolean)
        .join("\n");
    }
    default:
      return payload.summary || payload.title || "GridForge Studio view";
  }
}
