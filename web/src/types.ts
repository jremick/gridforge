import type { FeatureCollection } from "geojson";

export type PaperSize = "a4" | "letter" | "legal" | "a3";
export type Orientation = "portrait" | "landscape";
export type ColorMode = "color" | "bw";
export type Units = "metric" | "imperial";
export type DensityLevel = "low" | "normal" | "high";
export type HillshadeStrength = "off" | "light" | "normal" | "strong";
export type DemSourceKind = "auto" | "au-elvis" | "nz-linz" | "glo30" | "usgs3dep";
export type AUStateTrailKind =
  | "au-nsw-trails"
  | "au-vic-trails"
  | "au-tas-trails"
  | "au-qld-trails"
  | "au-wa-trails"
  | "au-sa-trails"
  | "au-nt-trails"
  | "au-act-trails";
export type AUOverlayKind =
  | "au-state-trails"
  | AUStateTrailKind
  | "au-hydro"
  | "au-parks"
  | "au-infra"
  | "au-aviation";
export type NZOverlayKind = "nz-doc-trails";
export type USOverlayKind = "fema" | "nhd" | "towers" | "enc";
export type WaypointCategory = "waypoint" | "camp" | "water" | "hazard" | "lz";
export type MBTilesFormat = "png" | "jpg";
export type GarminProfile = "standard" | "modern";
export type BasemapId = "opentopo" | "osm" | "satellite" | "offline";
export type PackDemTier = "srtm-1s" | "glo30" | "au-elvis";

export type JobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface CartoOptions {
  contour_density: DensityLevel;
  hillshade_strength: HillshadeStrength;
  label_density: DensityLevel;
  show_buildings: boolean;
  show_trees: boolean;
  show_power: boolean;
  show_pipelines: boolean;
  show_grid: boolean;
  show_river_arrows: boolean;
  show_slope_shading: boolean;
}

export interface OutputOptions {
  pdf: boolean;
  geopdf: boolean;
  mbtiles: boolean;
  mbtiles_format: MBTilesFormat;
  kmz: boolean;
  garmin_profile: GarminProfile;
  include_grid_on_digital: boolean;
}

export interface WaypointInput {
  label: string;
  lat: number;
  lon: number;
  category: WaypointCategory;
  notes?: string | null;
}

export interface ViewshedObserver {
  label: string;
  lat: number;
  lon: number;
  observer_height_m: number;
  target_height_m: number;
  max_distance_m: number;
}

export interface ViewshedPreviewResponse {
  observer_mgrs_10m: string;
  observer_elev_m: number | null;
  utm_epsg: number;
  max_distance_m: number;
  visible_area_km2: number;
  viewshed_geojson: FeatureCollection;
  range_ring_geojson: FeatureCollection;
}

export interface ViewshedEntry {
  id: string;
  observer: ViewshedObserver;
  loading: boolean;
  error: string | null;
  preview: ViewshedPreviewResponse | null;
}

export interface BasemapInfoResponse {
  available: boolean;
  name: string | null;
  format: "png" | "jpg" | null;
  minzoom: number;
  maxzoom: number;
  bounds: number[] | null;
  attribution: string | null;
}

export interface RouteMetricsWaypoint {
  seq: number;
  code: string;
  label: string;
  category: WaypointCategory;
  lat: number;
  lon: number;
  mgrs_10m: string;
  utm_epsg: number;
  elev_m?: number | null;
}

export interface RouteMetricsLeg {
  from_code: string;
  to_code: string;
  leg_dist_km: number;
  leg_dist_mi: number;
  cum_dist_km: number;
  true_az_deg: number;
  grid_az_deg: number;
  mag_az_deg: number;
  mag_az_mils: number;
}

export interface RouteMetricsResponse {
  waypoints: RouteMetricsWaypoint[];
  legs: RouteMetricsLeg[];
  total_distance_km: number;
  total_distance_mi: number;
}

export interface HealthResponse {
  status: string;
  queue_depth: number;
  worker_alive: boolean;
  worker_last_seen: string | null;
  memory_budget_mb: number;
  packs_count: number;
}

export interface BBox {
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
}

export interface StripSheetPlan {
  sheet_id: string;
  index: number;
  center_lat: number;
  center_lon: number;
  center_mgrs_10m: string;
  utm_bbox: BBox;
}

export interface StripPlanResponse {
  utm_epsg: number;
  utm_zone: number;
  utm_hemisphere: "N" | "S";
  gzd: string;
  route_distance_km: number;
  sheet_count?: number;
  sheets: StripSheetPlan[];
  route_geojson: FeatureCollection;
  footprint_geojson: FeatureCollection;
  grid_geojson: FeatureCollection;
}

export interface PackSummary {
  pack_id: string;
  name: string;
  region: string;
  bbox_wgs84: BBox;
  utm_zones: string[];
  vertical_datum: string;
  size_bytes: number;
}

export interface PacksResponse {
  packs: PackSummary[];
}

export interface PackRegionPreset {
  region_id: string;
  name: string;
  kind: "state" | "tactical";
  bbox_wgs84: BBox;
  utm_zones: string[];
  description?: string;
}

export interface PackBuildRequest {
  region_or_bbox: string;
  name?: string | null;
  include_osm: boolean;
  include_dem: boolean;
  include_overlays: boolean;
  dem_tier: PackDemTier;
}

export interface PackBuildTask {
  task_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  region: string;
  stage?: string | null;
  message?: string | null;
  elapsed_seconds?: number | null;
  error?: string | null;
  manifest?: PackSummary | null;
}

export interface GeocodeResult {
  display_name: string;
  short_name: string;
  lat: number;
  lon: number;
  type: string;
}

export interface GeocodeResponse {
  results: GeocodeResult[];
}

export interface DeclinationResponse {
  magnetic_declination_deg: number;
  annual_change_deg: number;
  grid_convergence_deg: number;
  gm_angle_deg: number;
  gm_angle_mils: number;
  model_name: string;
  decimal_year: number;
}

export interface FootprintRequest {
  center: string;
  scale: number;
  paper: PaperSize;
  orientation: Orientation;
  units: Units;
  date_iso?: string | null;
  grid_cols?: number;
  grid_rows?: number;
}

export interface FootprintResponse {
  center_lat: number;
  center_lon: number;
  center_mgrs_10m: string;
  utm_epsg: number;
  utm_zone: number;
  utm_hemisphere: "N" | "S";
  gzd: string;
  square_ids_100km: string[];
  page_width_mm: number;
  page_height_mm: number;
  frame_width_mm: number;
  frame_height_mm: number;
  ground_width_km: number;
  ground_height_km: number;
  grid_cols?: number;
  grid_rows?: number;
  sheet_count?: number;
  total_ground_width_km?: number;
  total_ground_height_km?: number;
  grid_spacing_mm: number;
  declination: DeclinationResponse;
  footprint_geojson: FeatureCollection;
  grid_geojson: FeatureCollection;
}

export interface CreateJobPayload {
  center: string;
  scale: number;
  paper: PaperSize;
  orientation: Orientation;
  color_mode: ColorMode;
  units: Units;
  title: string | null;
  date_iso?: string | null;
  dem_source: DemSourceKind;
  pack_id: string | null;
  au_overlays: AUOverlayKind[];
  nz_overlays: NZOverlayKind[];
  us_overlays: USOverlayKind[];
  carto: CartoOptions;
  outputs: OutputOptions;
  grid_cols: number;
  grid_rows: number;
  waypoints: WaypointInput[];
  viewsheds: ViewshedObserver[];
  strip_sheets?: StripSheetPlan[];
  include_calibration_page: boolean;
  include_route_card: boolean;
  include_elevation_profile: boolean;
  include_atlas_index: boolean;
  show_fold_marks: boolean;
}

export interface JobArtifact {
  kind: string;
  filename: string;
  size_bytes: number;
  download_url?: string;
}

export interface JobRecord {
  job_id: string;
  status: JobStatus;
  stage: string;
  progress_pct: number;
  title: string | null;
  center: string;
  center_mgrs?: string | null;
  scale: number;
  paper: PaperSize;
  orientation: Orientation;
  color_mode: ColorMode;
  grid_cols?: number;
  grid_rows?: number;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  elapsed_seconds?: number | null;
  peak_rss_mb?: number | null;
  error?: string | null;
  artifacts: JobArtifact[];
}

export interface AppFormState {
  center: string;
  lat: number;
  lon: number;
  title: string;
  scale: number;
  paper: PaperSize;
  orientation: Orientation;
  color_mode: ColorMode;
  units: Units;
  grid_cols: number;
  grid_rows: number;
  dem_source: DemSourceKind;
  pack_id: string;
  au_overlays: AUOverlayKind[];
  nz_overlays: NZOverlayKind[];
  us_overlays: USOverlayKind[];
  carto: CartoOptions;
  outputs: OutputOptions;
  overlayFile: File | null;
  waypoints: WaypointInput[];
  viewsheds: ViewshedEntry[];
  viewshedDefaults: {
    observer_height_m: number;
    target_height_m: number;
    max_distance_km: number;
  };
  stripPlan: StripPlanResponse | null;
  include_calibration_page: boolean;
  include_route_card: boolean;
  include_elevation_profile: boolean;
  include_atlas_index: boolean;
  show_fold_marks: boolean;
}

export interface LocationPreset {
  id: string;
  label: string;
  regionTag: string;
  lat: number;
  lon: number;
  scale: number;
  paper: PaperSize;
  units: Units;
  dem_source?: DemSourceKind;
  au_overlays?: AUOverlayKind[];
  defaultTitle: string;
  zoom: number;
}

export interface AtlasGridOption {
  id: string;
  label: string;
  cols: number;
  rows: number;
}
