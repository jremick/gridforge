import type { FeatureCollection } from "geojson";

export type PaperSize = "a4" | "letter" | "legal" | "a3";
export type Orientation = "portrait" | "landscape";
export type ColorMode = "color" | "bw" | "night-red" | "alpine";
export type Units = "metric" | "imperial";
export type DensityLevel = "low" | "normal" | "high";
export type HillshadeStrength = "off" | "light" | "normal" | "strong";
export type DemSourceKind =
  | "auto"
  | "au-elvis"
  | "nz-linz"
  | "ca-hrdem"
  | "glo30"
  | "usgs3dep";
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
export type CAOverlayKind = "ca-canvec-trails";
export type USOverlayKind =
  | "us-trails"
  | "us-public-lands"
  | "fema"
  | "nhd"
  | "towers"
  | "enc";
export type WaypointCategory = "waypoint" | "camp" | "water" | "hazard" | "lz";
export type MBTilesFormat = "png" | "jpg";
export type GarminProfile = "standard" | "modern";
export type BasemapId = "vector" | "opentopo" | "osm" | "satellite" | "offline";
export type PackDemTier =
  | "auto"
  | "srtm-1s"
  | "glo30"
  | "au-elvis"
  | "nz-linz"
  | "usgs3dep"
  | "ca-hrdem";
export type PresetRegionTab = "nsw-vic" | "au" | "nz" | "na";
export type PackRegionGroup = "au_tactical" | "au_state" | "nz" | "na";
export type TacticalGraphicKind =
  | "bearing_line"
  | "range_ring"
  | "corridor"
  | "sector";

export type MapInteractionMode =
  | "pan"
  | "markup"
  | "los"
  | "rf-link"
  | "ruler"
  | "ring"
  | "sector";

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
  gpkg_tiles: boolean;
  osmand_sqlitedb: boolean;
  field_bundle_zip: boolean;
  export_dpi: number;
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

export interface RFLinkSpec {
  from_lat: number;
  from_lon: number;
  to_lat: number;
  to_lon: number;
  from_height_m: number;
  to_height_m: number;
  freq_mhz: number;
  k_factor: number;
  from_label: string;
  to_label: string;
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

export interface RFLinkProfileResponse {
  status: "CLEAR" | "MARGINAL" | "OBSTRUCTED";
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
  samples: RFLinkSample[];
}

export interface RFLinkEntry {
  id: string;
  spec: RFLinkSpec;
  loading: boolean;
  error: string | null;
  profile: RFLinkProfileResponse | null;
}

export interface TacticalGraphic {
  kind: TacticalGraphicKind;
  label: string;
  points: [number, number][];
  radii_km: number[];
  color?: string | null;
  notes?: string | null;
}

export interface RulerSegment {
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

export interface RulerMetricsResponse {
  total_distance_m: number;
  segments: RulerSegment[];
}

export interface BasemapInfoResponse {
  available: boolean;
  name: string | null;
  format: "png" | "jpg" | null;
  minzoom: number;
  maxzoom: number;
  bounds: number[] | null;
  attribution: string | null;
  pack_id?: string | null;
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
  has_basemap?: boolean;
}

export interface PacksResponse {
  packs: PackSummary[];
}

export interface PackRegionPreset {
  region_id: string;
  name: string;
  group: PackRegionGroup;
  kind: "state" | "tactical";
  bbox_wgs84: BBox;
  utm_zones: string[];
  description?: string;
  estimated_size_hint?: string;
}

export interface PackBuildRequest {
  region_or_bbox: string;
  name?: string | null;
  include_osm: boolean;
  include_dem: boolean;
  include_overlays: boolean;
  include_basemap: boolean;
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

export interface SavedProject {
  project_id: string;
  name: string;
  description?: string | null;
  notes?: string | null;
  created_at: string;
  updated_at: string;
  last_job_id?: string | null;
  thumbnail_url?: string | null;
  state: Record<string, unknown>;
}

export interface SharedSnapshot {
  share_id: string;
  share_path: string;
  name: string;
  notes: string;
  state: Record<string, unknown>;
  view_count: number;
  created_at: string;
  expires_at: string | null;
}

export interface SharedSnapshotSummary {
  share_id: string;
  share_path: string;
  name: string;
  notes: string;
  source_project_id: string | null;
  view_count: number;
  created_at: string;
  expires_at: string | null;
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
  dpi: number;
  title: string | null;
  date_iso?: string | null;
  dem_source: DemSourceKind;
  pack_id: string | null;
  au_overlays: AUOverlayKind[];
  nz_overlays: NZOverlayKind[];
  ca_overlays: CAOverlayKind[];
  us_overlays: USOverlayKind[];
  carto: CartoOptions;
  outputs: OutputOptions;
  grid_cols: number;
  grid_rows: number;
  waypoints: WaypointInput[];
  viewsheds: ViewshedObserver[];
  rf_links: RFLinkSpec[];
  tactical_graphics: TacticalGraphic[];
  strip_sheets?: StripSheetPlan[];
  include_calibration_page: boolean;
  include_route_card: boolean;
  include_elevation_profile: boolean;
  include_atlas_index: boolean;
  include_rf_link_card: boolean;
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
  dpi: number;
  grid_cols: number;
  grid_rows: number;
  dem_source: DemSourceKind;
  pack_id: string;
  au_overlays: AUOverlayKind[];
  nz_overlays: NZOverlayKind[];
  ca_overlays: CAOverlayKind[];
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
  rfLinks: RFLinkEntry[];
  rfLinkDefaults: {
    freq_mhz: number;
    from_height_m: number;
    to_height_m: number;
  };
  tacticalGraphics: TacticalGraphic[];
  stripPlan: StripPlanResponse | null;
  include_calibration_page: boolean;
  include_route_card: boolean;
  include_elevation_profile: boolean;
  include_atlas_index: boolean;
  include_rf_link_card: boolean;
  show_fold_marks: boolean;
}

export interface LocationPreset {
  id: string;
  tab: PresetRegionTab;
  label: string;
  regionTag: string;
  lat: number;
  lon: number;
  scale: number;
  paper: PaperSize;
  units: Units;
  dem_source?: DemSourceKind;
  au_overlays?: AUOverlayKind[];
  nz_overlays?: NZOverlayKind[];
  ca_overlays?: CAOverlayKind[];
  us_overlays?: USOverlayKind[];
  defaultTitle: string;
  zoom: number;
}

export interface AtlasGridOption {
  id: string;
  label: string;
  cols: number;
  rows: number;
}

export type AuthMode = "none" | "hosted";
export type UserRole = "none" | "guest" | "friend" | "admin";
export type AccountRole = "friend" | "admin";
export type OutputKey =
  | "pdf"
  | "geopdf"
  | "mbtiles"
  | "kmz"
  | "gpkg_tiles"
  | "osmand_sqlitedb"
  | "field_bundle_zip";

export interface SessionUser {
  user_id: string;
  email: string;
  display_name: string | null;
  role: AccountRole;
}

export interface SessionCapabilities {
  max_grid_cells: number;
  max_grid_cols: number;
  max_grid_rows: number;
  strip_atlas_allowed: boolean;
  max_strip_sheets: number;
  allowed_outputs: string[];
  packs_manage_allowed: boolean;
  max_upload_bytes: number;
  max_dpi?: number;
  max_export_dpi?: number;
  turnstile_required: boolean;
  turnstile_sitekey: string | null;
  google_oauth_enabled: boolean;
}

export interface SessionQuotas {
  daily_renders_limit: number | null;
  daily_renders_used: number;
  daily_renders_remaining: number | null;
  hourly_previews_limit: number | null;
  hourly_previews_used: number;
  hourly_previews_remaining: number | null;
  public_renders_paused: boolean;
  global_daily_public_limit: number | null;
  global_daily_public_used: number;
}

export interface SessionResponse {
  auth_mode: AuthMode;
  authenticated: boolean;
  user: SessionUser | null;
  role: UserRole;
  workspace_scope?: string | null;
  capabilities: SessionCapabilities;
  quotas: SessionQuotas;
  brand_name?: string;
  brand_subtitle?: string;
  brand_tagline?: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface AcceptInviteRequest {
  token: string;
  email?: string;
  password: string;
  display_name?: string;
}

export interface AdminUsageSummary {
  renders_today: number;
  renders_7d: number;
  previews_today: number;
  active_queue_depth: number;
  global_daily_public_used: number;
  global_daily_public_limit: number | null;
  public_renders_paused: boolean;
  avg_elapsed_seconds: number | null;
  max_peak_rss_mb: number | null;
}

export interface AdminUsageRoleRow {
  role?: string;
  renders_today?: number;
  renders_7d?: number;
  previews_today?: number;
  [key: string]: number | string | undefined;
}

export interface AdminUsageEvent {
  event_id?: number | string;
  created_at: string;
  event_type: string;
  actor_role?: UserRole | string;
  actor_id?: string | null;
  job_id?: string | null;
  role?: UserRole | string;
  owner_id?: string | null;
  endpoint?: string | null;
  status_code?: number | null;
  detail?: Record<string, unknown> | string | null;
  elapsed_seconds?: number | null;
  peak_rss_mb?: number | null;
  [key: string]: unknown;
}

export interface AdminUsageResponse {
  summary: AdminUsageSummary;
  by_role: Record<string, AdminUsageRoleRow | number>;
  recent_events: AdminUsageEvent[];
}

export interface AdminUserRecord {
  user_id: string;
  email: string;
  display_name: string | null;
  role: AccountRole;
  disabled: boolean;
  daily_render_limit: number | null;
  hourly_preview_limit: number | null;
  has_password?: boolean;
  has_google?: boolean;
  renders_today?: number;
  created_at: string;
  last_login_at?: string | null;
}

export interface AdminInviteRecord {
  invite_id: string;
  email: string | null;
  role: AccountRole;
  expires_at: string;
  created_at?: string;
  used_at?: string | null;
  accepted_at?: string | null;
  revoked_at?: string | null;
  status?: "pending" | "used" | "revoked" | "expired";
}

export interface AdminUsersResponse {
  users: AdminUserRecord[];
  invites: AdminInviteRecord[];
}

export interface CreateInviteRequest {
  email?: string | null;
  role?: AccountRole;
  expires_hours?: number;
}

export interface CreateInviteResponse {
  invite_id: string;
  token: string;
  invite_path: string;
  expires_at: string;
}

export interface RevokeInviteResponse {
  revoked: boolean;
  invite_id: string;
}

export interface UpdateUserRequest {
  disabled?: boolean;
  daily_render_limit?: number | null;
  hourly_preview_limit?: number | null;
  role?: AccountRole;
}

export interface UpdateUserResponse {
  user: AdminUserRecord;
}

export interface AdminSettingsRequest {
  public_renders_paused: boolean;
}

export interface AdminSettingsResponse {
  public_renders_paused: boolean;
}
