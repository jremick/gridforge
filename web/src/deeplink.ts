import {
  ALL_AU_OVERLAY_KINDS,
  BASEMAP_OPTIONS,
  CA_OVERLAY_OPTIONS,
  COLOR_MODE_OPTIONS,
  DEM_SOURCE_OPTIONS,
  DENSITY_OPTIONS,
  DPI_OPTIONS,
  HILLSHADE_OPTIONS,
  NZ_OVERLAY_OPTIONS,
  PAPER_OPTIONS,
  SCALE_OPTIONS,
  US_OVERLAY_OPTIONS,
  WAYPOINT_CATEGORY_OPTIONS,
  createInitialFormState,
  parseLatLon,
} from "./presets.ts";
import type {
  AUOverlayKind,
  AppFormState,
  BasemapId,
  CAOverlayKind,
  ColorMode,
  DemSourceKind,
  DensityLevel,
  GarminProfile,
  HillshadeStrength,
  MBTilesFormat,
  NZOverlayKind,
  Orientation,
  PaperSize,
  RFLinkSpec,
  TacticalGraphic,
  TacticalGraphicKind,
  USOverlayKind,
  Units,
  ViewshedObserver,
  WaypointCategory,
  WaypointInput,
} from "./types.ts";

export const DEFAULT_BASEMAP: BasemapId = "opentopo";
export const MAX_DIRECT_URL_LENGTH = 3800;

const MAX_URL_WAYPOINTS = 50;
const MAX_URL_VIEWSHEDS = 4;
const MAX_URL_RF_LINKS = 10;
const MAX_URL_TACTICAL = 20;
const MAX_TACTICAL_POINTS = 64;
const MAX_TACTICAL_RADII = 8;

const MAX_SNAPSHOT_WAYPOINTS = 200;
const MAX_SNAPSHOT_VIEWSHEDS = 10;
const MAX_SNAPSHOT_RF_LINKS = 20;
const MAX_SNAPSHOT_TACTICAL = 50;

const VALID_SCALES = new Set(SCALE_OPTIONS.map((s) => s.value));
const VALID_DPIS = new Set(DPI_OPTIONS.map((d) => d.value));
const VALID_PAPERS = new Set<string>(PAPER_OPTIONS.map((p) => p.value));
const VALID_MODES = new Set<string>(COLOR_MODE_OPTIONS.map((m) => m.value));
const VALID_DEMS = new Set<string>(DEM_SOURCE_OPTIONS.map((d) => d.value));
const VALID_AU_OV = new Set<string>(ALL_AU_OVERLAY_KINDS);
const VALID_NZ_OV = new Set<string>(NZ_OVERLAY_OPTIONS.map((o) => o.value));
const VALID_CA_OV = new Set<string>(CA_OVERLAY_OPTIONS.map((o) => o.value));
const VALID_US_OV = new Set<string>(US_OVERLAY_OPTIONS.map((o) => o.value));
const VALID_DENSITIES = new Set<string>(DENSITY_OPTIONS.map((d) => d.value));
const VALID_HILLSHADES = new Set<string>(HILLSHADE_OPTIONS.map((h) => h.value));
const VALID_BASEMAPS = new Set<string>([
  ...BASEMAP_OPTIONS.map((b) => b.id),
  "offline",
]);
const VALID_WP_CATEGORIES = new Set<string>(
  WAYPOINT_CATEGORY_OPTIONS.map((w) => w.value)
);
const VALID_TACTICAL_KINDS = new Set<string>([
  "bearing_line",
  "range_ring",
  "corridor",
  "sector",
]);

const SHARE_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
const PACK_ID_RE = /^[a-zA-Z0-9_-]{1,64}$/;
const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;

const HIDE_CARTO_KEYS = [
  { token: "buildings", key: "show_buildings" },
  { token: "trees", key: "show_trees" },
  { token: "power", key: "show_power" },
  { token: "pipelines", key: "show_pipelines" },
  { token: "grid", key: "show_grid" },
  { token: "arrows", key: "show_river_arrows" },
] as const;

const OUTPUT_TOKEN_MAP = [
  { token: "pdf", key: "pdf" },
  { token: "geopdf", key: "geopdf" },
  { token: "mbtiles", key: "mbtiles" },
  { token: "kmz", key: "kmz" },
  { token: "gpkg", key: "gpkg_tiles" },
  { token: "osmand", key: "osmand_sqlitedb" },
  { token: "bundle", key: "field_bundle_zip" },
] as const;

export type OmittedUrlGroup = "tactical" | "rf_links" | "waypoints";

export interface UrlViewState {
  basemap: BasemapId;
  zoom: number | null;
}

export interface UrlHydrationResult {
  hasCustomCenter: boolean;
  needsFlyToResolvedCenter: boolean;
  basemap: BasemapId;
  hasCustomBasemap: boolean;
  zoom: number | null;
  shareId: string | null;
}

export interface SerializedUrlResult {
  params: URLSearchParams;
  queryString: string;
  truncated: boolean;
  omittedGroups: OmittedUrlGroup[];
}

export interface SnapshotHydrationIds {
  nextViewshedId: () => string;
  nextRFLinkId: () => string;
}

export interface SnapshotHydrationResult {
  basemap: BasemapId | null;
  zoom: number | null;
}

function isValidLatLon(lat: number, lon: number): boolean {
  return (
    Number.isFinite(lat) &&
    Number.isFinite(lon) &&
    lat >= -80 &&
    lat <= 84 &&
    lon >= -180 &&
    lon <= 180
  );
}

function clampFinite(value: number, min: number, max: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

function formatCompactNumber(value: number, decimals: number): string {
  return String(Number(value.toFixed(decimals)));
}

function escapeDelimitedField(raw: string): string {
  return raw
    .replace(/%/g, "%25")
    .replace(/,/g, "%2C")
    .replace(/;/g, "%3B");
}

function unescapeDelimitedField(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

function encodeBase64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]!);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeBase64Url(encoded: string): string | null {
  if (!encoded || !/^[A-Za-z0-9_-]+$/.test(encoded)) return null;
  const padded =
    encoded.replace(/-/g, "+").replace(/_/g, "/") +
    "=".repeat((4 - (encoded.length % 4)) % 4);
  try {
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new TextDecoder().decode(bytes);
  } catch {
    return null;
  }
}

function parseBoolParam(raw: string | null): boolean | null {
  if (raw === null) return null;
  const norm = raw.trim().toLowerCase();
  if (norm === "1" || norm === "true") return true;
  if (norm === "0" || norm === "false") return false;
  return null;
}

function sanitizeTacticalGraphic(raw: unknown): TacticalGraphic | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const rawKind =
    typeof obj.kind === "string"
      ? obj.kind
      : typeof obj.k === "string"
        ? obj.k
        : "";
  if (!VALID_TACTICAL_KINDS.has(rawKind)) return null;
  const kind = rawKind as TacticalGraphicKind;

  const rawPoints = Array.isArray(obj.points)
    ? obj.points
    : Array.isArray(obj.p)
      ? obj.p
      : null;
  if (!rawPoints) return null;

  const points: [number, number][] = [];
  for (const pt of rawPoints.slice(0, MAX_TACTICAL_POINTS)) {
    if (!Array.isArray(pt) || pt.length < 2) continue;
    const lat = Number(pt[0]);
    const lon = Number(pt[1]);
    if (!isValidLatLon(lat, lon)) continue;
    points.push([Number(lat.toFixed(6)), Number(lon.toFixed(6))]);
  }

  const minPoints =
    kind === "sector" ? 3 : kind === "range_ring" ? 1 : 2;
  if (points.length < minPoints) return null;

  const rawRadii = Array.isArray(obj.radii_km)
    ? obj.radii_km
    : Array.isArray(obj.r)
      ? obj.r
      : [];
  const radii: number[] = [];
  for (const rVal of rawRadii.slice(0, MAX_TACTICAL_RADII)) {
    const r = Number(rVal);
    if (Number.isFinite(r) && r > 0 && r <= 100) {
      radii.push(Number(r.toFixed(2)));
    }
  }
  if (kind === "range_ring" && radii.length === 0) {
    radii.push(1, 3, 5);
  }

  const rawLabel =
    typeof obj.label === "string"
      ? obj.label
      : typeof obj.l === "string"
        ? obj.l
        : "";
  const label = rawLabel.trim().slice(0, 48) || kind.toUpperCase();

  const rawColor =
    typeof obj.color === "string"
      ? obj.color
      : typeof obj.c === "string"
        ? obj.c
        : null;
  const color =
    rawColor && HEX_COLOR_RE.test(rawColor.trim()) ? rawColor.trim() : undefined;

  const rawNotes =
    typeof obj.notes === "string"
      ? obj.notes
      : typeof obj.n === "string"
        ? obj.n
        : null;
  const notes = rawNotes?.trim() ? rawNotes.trim().slice(0, 160) : undefined;

  const graphic: TacticalGraphic = {
    kind,
    label,
    points,
    radii_km: radii,
  };
  if (color) graphic.color = color;
  if (notes) graphic.notes = notes;
  return graphic;
}

export function extractShareIdFromSearch(search: string): string | null {
  const params = new URLSearchParams(search);
  const raw = params.get("share")?.trim() ?? "";
  return SHARE_ID_RE.test(raw) ? raw : null;
}

export function hydrateStateFromSearch(
  search: string,
  state: AppFormState
): UrlHydrationResult {
  const params = new URLSearchParams(search);
  const shareId = extractShareIdFromSearch(search);

  let hasCustomCenter = false;
  let needsFlyToResolvedCenter = false;
  let basemap: BasemapId = DEFAULT_BASEMAP;
  let hasCustomBasemap = false;
  let zoom: number | null = null;

  const centerParam = params.get("center")?.trim();
  if (centerParam) {
    hasCustomCenter = true;
    state.center = centerParam;
    const parsed = parseLatLon(centerParam);
    if (parsed) {
      state.lat = parsed.lat;
      state.lon = parsed.lon;
    } else {
      needsFlyToResolvedCenter = true;
    }
  }

  const bmParam = params.get("bm")?.trim().toLowerCase();
  if (bmParam && VALID_BASEMAPS.has(bmParam)) {
    basemap = bmParam as BasemapId;
    hasCustomBasemap = true;
  }

  const zRaw = params.get("z");
  if (zRaw !== null && zRaw.trim() !== "") {
    const zVal = Number(zRaw);
    if (Number.isFinite(zVal)) {
      zoom = Number(clampFinite(zVal, 2, 20, 12).toFixed(2));
    }
  }

  const scaleParam = Number(params.get("scale"));
  if (VALID_SCALES.has(scaleParam)) {
    state.scale = scaleParam;
  }

  const paperParam = params.get("paper")?.toLowerCase();
  if (paperParam && VALID_PAPERS.has(paperParam)) {
    state.paper = paperParam as PaperSize;
  }

  const orientParam = params.get("orientation")?.toLowerCase();
  if (orientParam === "portrait" || orientParam === "landscape") {
    state.orientation = orientParam as Orientation;
  }

  const modeParam = params.get("mode")?.toLowerCase();
  if (modeParam && VALID_MODES.has(modeParam)) {
    state.color_mode = modeParam as ColorMode;
  }

  const unitsParam = params.get("units")?.toLowerCase();
  if (unitsParam === "metric" || unitsParam === "imperial") {
    state.units = unitsParam as Units;
  }

  const dpiParam = Number(params.get("dpi"));
  if (VALID_DPIS.has(dpiParam)) {
    state.dpi = dpiParam;
  }

  const exportDpiParam = Number(params.get("export_dpi"));
  if (VALID_DPIS.has(exportDpiParam)) {
    state.outputs.export_dpi = exportDpiParam;
  }

  const demParam = params.get("dem")?.toLowerCase();
  if (demParam && VALID_DEMS.has(demParam)) {
    state.dem_source = demParam as DemSourceKind;
  }

  const packParam = params.get("pack")?.trim();
  if (packParam && PACK_ID_RE.test(packParam)) {
    state.pack_id = packParam;
  }

  const gridParam = params.get("grid")?.toLowerCase();
  if (gridParam) {
    const match = /^([1-3])x([1-3])$/.exec(gridParam);
    if (match) {
      state.grid_cols = Number(match[1]);
      state.grid_rows = Number(match[2]);
      if (state.grid_cols * state.grid_rows > 1) {
        state.include_atlas_index = true;
      }
    }
  }

  const titleParam = params.get("title")?.trim();
  if (titleParam) {
    state.title = titleParam.slice(0, 80);
  }

  const auParam = params.get("au")?.trim();
  if (auParam) {
    state.au_overlays = auParam
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s): s is AUOverlayKind => VALID_AU_OV.has(s));
  }

  const nzParam = params.get("nz")?.trim();
  if (nzParam) {
    state.nz_overlays = nzParam
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s): s is NZOverlayKind => VALID_NZ_OV.has(s));
  }

  const caParam = params.get("ca")?.trim();
  if (caParam) {
    state.ca_overlays = caParam
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s): s is CAOverlayKind => VALID_CA_OV.has(s));
  }

  const usParam = params.get("us")?.trim();
  if (usParam) {
    state.us_overlays = usParam
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s): s is USOverlayKind => VALID_US_OV.has(s));
  }

  // Cartography options
  const contoursParam = params.get("contours")?.trim().toLowerCase();
  if (contoursParam && VALID_DENSITIES.has(contoursParam)) {
    state.carto.contour_density = contoursParam as DensityLevel;
  }

  const hillshadeParam = params.get("hillshade")?.trim().toLowerCase();
  if (hillshadeParam && VALID_HILLSHADES.has(hillshadeParam)) {
    state.carto.hillshade_strength = hillshadeParam as HillshadeStrength;
  }

  const labelsParam = params.get("labels")?.trim().toLowerCase();
  if (labelsParam && VALID_DENSITIES.has(labelsParam)) {
    state.carto.label_density = labelsParam as DensityLevel;
  }

  const hideParam = params.get("hide")?.trim();
  if (hideParam) {
    const hidden = new Set(
      hideParam.split(",").map((s) => s.trim().toLowerCase())
    );
    for (const item of HIDE_CARTO_KEYS) {
      if (hidden.has(item.token)) {
        state.carto[item.key] = false;
      }
    }
  }

  const slopeVal = parseBoolParam(params.get("slope"));
  if (slopeVal !== null) {
    state.carto.show_slope_shading = slopeVal;
  }

  // Output format options
  const outParam = params.get("out")?.trim();
  if (outParam) {
    const requested = new Set(
      outParam
        .split(",")
        .map((s) => s.trim().toLowerCase())
        .filter(Boolean)
    );
    const recognized = OUTPUT_TOKEN_MAP.some((item) =>
      requested.has(item.token)
    );
    if (recognized) {
      for (const item of OUTPUT_TOKEN_MAP) {
        state.outputs[item.key] = requested.has(item.token);
      }
      if (!state.outputs.pdf) {
        state.outputs.geopdf = false;
      }
      const hasPrimary =
        state.outputs.pdf ||
        state.outputs.mbtiles ||
        state.outputs.kmz ||
        state.outputs.gpkg_tiles ||
        state.outputs.osmand_sqlitedb ||
        state.outputs.field_bundle_zip;
      if (!hasPrimary) {
        state.outputs.pdf = true;
      }
    }
  }

  const mbfmtParam = params.get("mbfmt")?.trim().toLowerCase();
  if (mbfmtParam === "png" || mbfmtParam === "jpg") {
    state.outputs.mbtiles_format = mbfmtParam as MBTilesFormat;
  }

  const garminParam = params.get("garmin")?.trim().toLowerCase();
  if (garminParam === "standard" || garminParam === "modern") {
    state.outputs.garmin_profile = garminParam as GarminProfile;
  }

  const dgridVal = parseBoolParam(params.get("dgrid"));
  if (dgridVal !== null) {
    state.outputs.include_grid_on_digital = dgridVal;
  }

  // Booklet / print supplement options
  const calVal = parseBoolParam(params.get("cal"));
  if (calVal !== null) {
    state.include_calibration_page = calVal;
  }
  const foldVal = parseBoolParam(params.get("fold"));
  if (foldVal !== null) {
    state.show_fold_marks = foldVal;
  }
  const indexVal = parseBoolParam(params.get("index"));
  if (indexVal !== null) {
    state.include_atlas_index = indexVal;
  }
  const rcVal = parseBoolParam(params.get("rc"));
  if (rcVal !== null) {
    state.include_route_card = rcVal;
  }
  const epVal = parseBoolParam(params.get("ep"));
  if (epVal !== null) {
    state.include_elevation_profile = epVal;
  }
  const rfcardVal = parseBoolParam(params.get("rfcard"));
  if (rfcardVal !== null) {
    state.include_rf_link_card = rfcardVal;
  }

  // LOS / Viewshed observers
  const losParam = params.get("los")?.trim();
  if (losParam) {
    const parts = losParam.split(";").slice(0, MAX_URL_VIEWSHEDS);
    parts.forEach((raw, idx) => {
      const segs = raw.split(",");
      const lat = Number(segs[0]);
      const lon = Number(segs[1]);
      if (!isValidLatLon(lat, lon)) {
        return;
      }
      const obsH =
        segs[2] !== undefined && segs[2] !== ""
          ? clampFinite(Number(segs[2]), 0, 500, 2)
          : 2;
      const tgtH =
        segs[3] !== undefined && segs[3] !== ""
          ? clampFinite(Number(segs[3]), 0, 500, 1.5)
          : 1.5;
      const maxKm =
        segs[4] !== undefined && segs[4] !== ""
          ? clampFinite(Number(segs[4]), 0.25, 25, 10)
          : 10;
      const customLabel =
        segs[5] !== undefined && segs[5] !== ""
          ? unescapeDelimitedField(segs[5]).trim().slice(0, 24)
          : "";
      state.viewsheds.push({
        id: `vs-init-${idx + 1}`,
        observer: {
          label: customLabel || `OBS${idx + 1}`,
          lat,
          lon,
          observer_height_m: obsH,
          target_height_m: tgtH,
          max_distance_m: maxKm * 1000,
        },
        loading: false,
        error: null,
        preview: null,
      });
    });
  }

  // Waypoints: lat,lon,category,label[,notes]
  const wpParam = params.get("wp")?.trim();
  if (wpParam) {
    const parts = wpParam.split(";").slice(0, MAX_URL_WAYPOINTS);
    parts.forEach((raw, idx) => {
      const segs = raw.split(",");
      const lat = Number(segs[0]);
      const lon = Number(segs[1]);
      if (!isValidLatLon(lat, lon)) return;
      const rawCat = (segs[2] ?? "").trim().toLowerCase();
      const category: WaypointCategory = VALID_WP_CATEGORIES.has(rawCat)
        ? (rawCat as WaypointCategory)
        : "waypoint";
      const label =
        segs[3] !== undefined && segs[3] !== ""
          ? unescapeDelimitedField(segs[3]).trim().slice(0, 40)
          : "";
      const notes =
        segs[4] !== undefined && segs[4] !== ""
          ? unescapeDelimitedField(segs[4]).trim().slice(0, 160)
          : "";
      const wp: WaypointInput = {
        label: label || `WP${idx + 1}`,
        lat: Number(lat.toFixed(6)),
        lon: Number(lon.toFixed(6)),
        category,
      };
      if (notes) wp.notes = notes;
      state.waypoints.push(wp);
    });
  }

  // RF point-to-point links: fromLat,fromLon,toLat,toLon,fromH,toH,freq,k[,fromLabel,toLabel]
  const rfParam = params.get("rf")?.trim();
  if (rfParam) {
    const parts = rfParam.split(";").slice(0, MAX_URL_RF_LINKS);
    parts.forEach((raw, idx) => {
      const segs = raw.split(",");
      const fromLat = Number(segs[0]);
      const fromLon = Number(segs[1]);
      const toLat = Number(segs[2]);
      const toLon = Number(segs[3]);
      if (!isValidLatLon(fromLat, fromLon) || !isValidLatLon(toLat, toLon)) {
        return;
      }
      const fromH =
        segs[4] !== undefined && segs[4] !== ""
          ? clampFinite(Number(segs[4]), 0, 500, 2)
          : 2;
      const toH =
        segs[5] !== undefined && segs[5] !== ""
          ? clampFinite(Number(segs[5]), 0, 500, 2)
          : 2;
      const freq =
        segs[6] !== undefined && segs[6] !== ""
          ? clampFinite(Number(segs[6]), 1, 100000, 915)
          : 915;
      const kFactor =
        segs[7] !== undefined && segs[7] !== ""
          ? clampFinite(Number(segs[7]), 0.5, 4, 1.333)
          : 1.333;
      const fromLabel =
        segs[8] !== undefined && segs[8] !== ""
          ? unescapeDelimitedField(segs[8]).trim().slice(0, 24)
          : `A${idx + 1}`;
      const toLabel =
        segs[9] !== undefined && segs[9] !== ""
          ? unescapeDelimitedField(segs[9]).trim().slice(0, 24)
          : `B${idx + 1}`;
      state.rfLinks.push({
        id: `rf-init-${idx + 1}`,
        spec: {
          from_lat: Number(fromLat.toFixed(6)),
          from_lon: Number(fromLon.toFixed(6)),
          to_lat: Number(toLat.toFixed(6)),
          to_lon: Number(toLon.toFixed(6)),
          from_height_m: fromH,
          to_height_m: toH,
          freq_mhz: freq,
          k_factor: kFactor,
          from_label: fromLabel || `A${idx + 1}`,
          to_label: toLabel || `B${idx + 1}`,
        },
        loading: false,
        error: null,
        profile: null,
      });
    });
  }

  // Tactical graphics (base64url compact JSON)
  const tacParam = params.get("tac")?.trim();
  if (tacParam) {
    const decoded = decodeBase64Url(tacParam);
    if (decoded) {
      try {
        const parsed = JSON.parse(decoded) as unknown;
        if (Array.isArray(parsed)) {
          for (const item of parsed.slice(0, MAX_URL_TACTICAL)) {
            const clean = sanitizeTacticalGraphic(item);
            if (clean) state.tacticalGraphics.push(clean);
          }
        }
      } catch {
        // Ignore malformed tactical param
      }
    }
  }

  return {
    hasCustomCenter,
    needsFlyToResolvedCenter,
    basemap,
    hasCustomBasemap,
    zoom,
    shareId,
  };
}

export function serializeStateToParams(
  state: AppFormState,
  view?: Partial<UrlViewState>,
  options?: { maxLength?: number; basePathLength?: number }
): SerializedUrlResult {
  const params = new URLSearchParams();
  const maxLength = options?.maxLength ?? MAX_DIRECT_URL_LENGTH;
  const basePathLength = options?.basePathLength ?? 0;

  params.set("center", state.center.replace(/\s*,\s*/, ","));
  params.set("scale", String(state.scale));
  params.set("paper", state.paper);
  params.set("orientation", state.orientation);
  params.set("mode", state.color_mode);
  params.set("units", state.units);

  if (view?.basemap && view.basemap !== DEFAULT_BASEMAP && VALID_BASEMAPS.has(view.basemap)) {
    params.set("bm", view.basemap);
  }
  if (typeof view?.zoom === "number" && Number.isFinite(view.zoom)) {
    const zClamped = clampFinite(view.zoom, 2, 20, 12);
    if (Math.abs(zClamped - 12) >= 0.05) {
      params.set("z", formatCompactNumber(zClamped, 1));
    }
  }

  if (state.dpi !== 300) {
    params.set("dpi", String(state.dpi));
  }
  if (state.outputs.export_dpi !== 300) {
    params.set("export_dpi", String(state.outputs.export_dpi));
  }
  if (state.dem_source !== "auto") {
    params.set("dem", state.dem_source);
  }
  if (state.pack_id) {
    params.set("pack", state.pack_id);
  }
  const isMultiSheet = state.grid_cols * state.grid_rows > 1;
  if (isMultiSheet) {
    params.set("grid", `${state.grid_cols}x${state.grid_rows}`);
  }
  if (state.title.trim()) {
    params.set("title", state.title.trim());
  }
  if (state.au_overlays.length > 0) {
    params.set("au", state.au_overlays.join(","));
  }
  if (state.nz_overlays.length > 0) {
    params.set("nz", state.nz_overlays.join(","));
  }
  if (state.ca_overlays.length > 0) {
    params.set("ca", state.ca_overlays.join(","));
  }
  if (state.us_overlays.length > 0) {
    params.set("us", state.us_overlays.join(","));
  }

  // Cartography options (only non-defaults)
  if (state.carto.contour_density !== "normal") {
    params.set("contours", state.carto.contour_density);
  }
  if (state.carto.hillshade_strength !== "normal") {
    params.set("hillshade", state.carto.hillshade_strength);
  }
  if (state.carto.label_density !== "normal") {
    params.set("labels", state.carto.label_density);
  }
  const hiddenLayers = HIDE_CARTO_KEYS.filter(
    (item) => !state.carto[item.key]
  ).map((item) => item.token);
  if (hiddenLayers.length > 0) {
    params.set("hide", hiddenLayers.join(","));
  }
  if (state.carto.show_slope_shading) {
    params.set("slope", "1");
  }

  // Output format options (only non-defaults)
  const activeOutputs = OUTPUT_TOKEN_MAP.filter(
    (item) => state.outputs[item.key]
  ).map((item) => item.token);
  if (activeOutputs.length !== 1 || activeOutputs[0] !== "pdf") {
    if (activeOutputs.length > 0) {
      params.set("out", activeOutputs.join(","));
    }
  }
  if (state.outputs.mbtiles_format !== "png") {
    params.set("mbfmt", state.outputs.mbtiles_format);
  }
  if (state.outputs.garmin_profile !== "standard") {
    params.set("garmin", state.outputs.garmin_profile);
  }
  if (!state.outputs.include_grid_on_digital) {
    params.set("dgrid", "0");
  }

  // Booklet toggles
  if (state.include_calibration_page) {
    params.set("cal", "1");
  }
  if (state.show_fold_marks) {
    params.set("fold", "1");
  }
  if (isMultiSheet) {
    params.set("index", state.include_atlas_index ? "1" : "0");
  } else if (state.include_atlas_index) {
    params.set("index", "1");
  }
  if (state.include_route_card) {
    params.set("rc", "1");
  }
  if (state.include_elevation_profile) {
    params.set("ep", "1");
  }
  if (state.include_rf_link_card) {
    params.set("rfcard", "1");
  }

  // LOS / Viewshed observers
  if (state.viewsheds.length > 0) {
    const encoded = state.viewsheds
      .slice(0, MAX_URL_VIEWSHEDS)
      .map((v, idx) => {
        const o = v.observer;
        const base = `${formatCompactNumber(o.lat, 5)},${formatCompactNumber(o.lon, 5)},${o.observer_height_m},${o.target_height_m},${formatCompactNumber(o.max_distance_m / 1000, 2)}`;
        const defaultLabel = `OBS${idx + 1}`;
        const trimmedLabel = o.label.trim();
        if (trimmedLabel && trimmedLabel !== defaultLabel) {
          return `${base},${escapeDelimitedField(trimmedLabel.slice(0, 24))}`;
        }
        return base;
      })
      .join(";");
    params.set("los", encoded);
  }

  // Waypoints
  if (state.waypoints.length > 0) {
    const encoded = state.waypoints
      .slice(0, MAX_URL_WAYPOINTS)
      .map((wp) => {
        const base = `${formatCompactNumber(wp.lat, 5)},${formatCompactNumber(wp.lon, 5)},${wp.category},${escapeDelimitedField(wp.label.trim().slice(0, 40))}`;
        const notes = wp.notes?.trim();
        if (notes) {
          return `${base},${escapeDelimitedField(notes.slice(0, 160))}`;
        }
        return base;
      })
      .join(";");
    params.set("wp", encoded);
  }

  // RF Links
  if (state.rfLinks.length > 0) {
    const encoded = state.rfLinks
      .slice(0, MAX_URL_RF_LINKS)
      .map((entry, idx) => {
        const s = entry.spec;
        const base = `${formatCompactNumber(s.from_lat, 5)},${formatCompactNumber(s.from_lon, 5)},${formatCompactNumber(s.to_lat, 5)},${formatCompactNumber(s.to_lon, 5)},${s.from_height_m},${s.to_height_m},${s.freq_mhz},${s.k_factor}`;
        const defFrom = `A${idx + 1}`;
        const defTo = `B${idx + 1}`;
        const fl = s.from_label.trim();
        const tl = s.to_label.trim();
        if ((fl && fl !== defFrom) || (tl && tl !== defTo)) {
          return `${base},${escapeDelimitedField((fl || defFrom).slice(0, 24))},${escapeDelimitedField((tl || defTo).slice(0, 24))}`;
        }
        return base;
      })
      .join(";");
    params.set("rf", encoded);
  }

  // Tactical graphics
  if (state.tacticalGraphics.length > 0) {
    const compact = state.tacticalGraphics.slice(0, MAX_URL_TACTICAL).map((g) => {
      const rec: Record<string, unknown> = {
        k: g.kind,
        l: g.label.trim().slice(0, 48),
        p: g.points
          .slice(0, MAX_TACTICAL_POINTS)
          .map((pt) => [Number(pt[0].toFixed(5)), Number(pt[1].toFixed(5))]),
      };
      if (g.radii_km.length > 0) {
        rec.r = g.radii_km.slice(0, MAX_TACTICAL_RADII);
      }
      if (g.color && HEX_COLOR_RE.test(g.color)) {
        rec.c = g.color;
      }
      if (g.notes?.trim()) {
        rec.n = g.notes.trim().slice(0, 160);
      }
      return rec;
    });
    params.set("tac", encodeBase64Url(JSON.stringify(compact)));
  }

  // Enforce URL length budget gracefully by dropping heaviest optional markups first
  const omittedGroups: OmittedUrlGroup[] = [];
  if (state.tacticalGraphics.length > MAX_URL_TACTICAL) {
    omittedGroups.push("tactical");
  }
  if (state.rfLinks.length > MAX_URL_RF_LINKS) {
    omittedGroups.push("rf_links");
  }
  if (state.waypoints.length > MAX_URL_WAYPOINTS) {
    omittedGroups.push("waypoints");
  }

  const dropOrder: { param: string; group: OmittedUrlGroup }[] = [
    { param: "tac", group: "tactical" },
    { param: "rf", group: "rf_links" },
    { param: "wp", group: "waypoints" },
  ];

  let queryString = params.toString();
  for (const item of dropOrder) {
    if (basePathLength + 1 + queryString.length <= maxLength) break;
    if (params.has(item.param)) {
      params.delete(item.param);
      if (!omittedGroups.includes(item.group)) {
        omittedGroups.push(item.group);
      }
      queryString = params.toString();
    }
  }

  return {
    params,
    queryString,
    truncated: omittedGroups.length > 0,
    omittedGroups,
  };
}

export function buildDirectShareUrl(
  originAndPath: string,
  state: AppFormState,
  view?: Partial<UrlViewState>,
  maxLength = MAX_DIRECT_URL_LENGTH
): {
  url: string;
  truncated: boolean;
  omittedGroups: OmittedUrlGroup[];
} {
  const cleanBase = originAndPath.split("?")[0] ?? originAndPath;
  const serialized = serializeStateToParams(state, view, {
    maxLength,
    basePathLength: cleanBase.length,
  });
  return {
    url: serialized.queryString
      ? `${cleanBase}?${serialized.queryString}`
      : cleanBase,
    truncated: serialized.truncated,
    omittedGroups: serialized.omittedGroups,
  };
}

export function hydrateStateFromSnapshot(
  rawState: Record<string, unknown>,
  targetState: AppFormState,
  ids: SnapshotHydrationIds
): SnapshotHydrationResult {
  const defaults = createInitialFormState();
  const s = rawState;

  if (typeof s.center === "string" && s.center.trim()) {
    targetState.center = s.center.trim().slice(0, 120);
    const parsedCenter = parseLatLon(targetState.center);
    if (parsedCenter) {
      targetState.lat = parsedCenter.lat;
      targetState.lon = parsedCenter.lon;
    }
  } else {
    targetState.center = defaults.center;
    targetState.lat = defaults.lat;
    targetState.lon = defaults.lon;
  }

  if (
    typeof s.lat === "number" &&
    typeof s.lon === "number" &&
    isValidLatLon(s.lat, s.lon)
  ) {
    targetState.lat = s.lat;
    targetState.lon = s.lon;
  }

  targetState.title =
    typeof s.title === "string" ? s.title.slice(0, 80) : "";

  targetState.scale =
    typeof s.scale === "number" && VALID_SCALES.has(s.scale)
      ? s.scale
      : defaults.scale;

  targetState.paper =
    typeof s.paper === "string" && VALID_PAPERS.has(s.paper)
      ? (s.paper as PaperSize)
      : defaults.paper;

  targetState.orientation =
    s.orientation === "portrait" || s.orientation === "landscape"
      ? s.orientation
      : defaults.orientation;

  targetState.color_mode =
    typeof s.color_mode === "string" && VALID_MODES.has(s.color_mode)
      ? (s.color_mode as ColorMode)
      : defaults.color_mode;

  targetState.units =
    s.units === "metric" || s.units === "imperial" ? s.units : defaults.units;

  targetState.dpi =
    typeof s.dpi === "number" && VALID_DPIS.has(s.dpi) ? s.dpi : defaults.dpi;

  targetState.grid_cols =
    typeof s.grid_cols === "number" &&
    Number.isInteger(s.grid_cols) &&
    s.grid_cols >= 1 &&
    s.grid_cols <= 3
      ? s.grid_cols
      : defaults.grid_cols;

  targetState.grid_rows =
    typeof s.grid_rows === "number" &&
    Number.isInteger(s.grid_rows) &&
    s.grid_rows >= 1 &&
    s.grid_rows <= 3
      ? s.grid_rows
      : defaults.grid_rows;

  targetState.dem_source =
    typeof s.dem_source === "string" && VALID_DEMS.has(s.dem_source)
      ? (s.dem_source as DemSourceKind)
      : defaults.dem_source;

  targetState.pack_id =
    typeof s.pack_id === "string" && PACK_ID_RE.test(s.pack_id.trim())
      ? s.pack_id.trim()
      : "";

  targetState.au_overlays = Array.isArray(s.au_overlays)
    ? s.au_overlays.filter(
        (x): x is AUOverlayKind => typeof x === "string" && VALID_AU_OV.has(x)
      )
    : [];
  targetState.nz_overlays = Array.isArray(s.nz_overlays)
    ? s.nz_overlays.filter(
        (x): x is NZOverlayKind => typeof x === "string" && VALID_NZ_OV.has(x)
      )
    : [];
  targetState.ca_overlays = Array.isArray(s.ca_overlays)
    ? s.ca_overlays.filter(
        (x): x is CAOverlayKind => typeof x === "string" && VALID_CA_OV.has(x)
      )
    : [];
  targetState.us_overlays = Array.isArray(s.us_overlays)
    ? s.us_overlays.filter(
        (x): x is USOverlayKind => typeof x === "string" && VALID_US_OV.has(x)
      )
    : [];

  // Cartography options (strictly typed fallback)
  const rawCarto =
    s.carto && typeof s.carto === "object"
      ? (s.carto as Record<string, unknown>)
      : {};
  targetState.carto = {
    contour_density:
      typeof rawCarto.contour_density === "string" &&
      VALID_DENSITIES.has(rawCarto.contour_density)
        ? (rawCarto.contour_density as DensityLevel)
        : defaults.carto.contour_density,
    hillshade_strength:
      typeof rawCarto.hillshade_strength === "string" &&
      VALID_HILLSHADES.has(rawCarto.hillshade_strength)
        ? (rawCarto.hillshade_strength as HillshadeStrength)
        : defaults.carto.hillshade_strength,
    label_density:
      typeof rawCarto.label_density === "string" &&
      VALID_DENSITIES.has(rawCarto.label_density)
        ? (rawCarto.label_density as DensityLevel)
        : defaults.carto.label_density,
    show_buildings:
      typeof rawCarto.show_buildings === "boolean"
        ? rawCarto.show_buildings
        : defaults.carto.show_buildings,
    show_trees:
      typeof rawCarto.show_trees === "boolean"
        ? rawCarto.show_trees
        : defaults.carto.show_trees,
    show_power:
      typeof rawCarto.show_power === "boolean"
        ? rawCarto.show_power
        : defaults.carto.show_power,
    show_pipelines:
      typeof rawCarto.show_pipelines === "boolean"
        ? rawCarto.show_pipelines
        : defaults.carto.show_pipelines,
    show_grid:
      typeof rawCarto.show_grid === "boolean"
        ? rawCarto.show_grid
        : defaults.carto.show_grid,
    show_river_arrows:
      typeof rawCarto.show_river_arrows === "boolean"
        ? rawCarto.show_river_arrows
        : defaults.carto.show_river_arrows,
    show_slope_shading:
      typeof rawCarto.show_slope_shading === "boolean"
        ? rawCarto.show_slope_shading
        : defaults.carto.show_slope_shading,
  };

  // Output options
  const rawOut =
    s.outputs && typeof s.outputs === "object"
      ? (s.outputs as Record<string, unknown>)
      : {};
  targetState.outputs = {
    pdf: typeof rawOut.pdf === "boolean" ? rawOut.pdf : defaults.outputs.pdf,
    geopdf:
      typeof rawOut.geopdf === "boolean"
        ? rawOut.geopdf
        : defaults.outputs.geopdf,
    mbtiles:
      typeof rawOut.mbtiles === "boolean"
        ? rawOut.mbtiles
        : defaults.outputs.mbtiles,
    mbtiles_format:
      rawOut.mbtiles_format === "png" || rawOut.mbtiles_format === "jpg"
        ? rawOut.mbtiles_format
        : defaults.outputs.mbtiles_format,
    kmz: typeof rawOut.kmz === "boolean" ? rawOut.kmz : defaults.outputs.kmz,
    garmin_profile:
      rawOut.garmin_profile === "standard" || rawOut.garmin_profile === "modern"
        ? rawOut.garmin_profile
        : defaults.outputs.garmin_profile,
    include_grid_on_digital:
      typeof rawOut.include_grid_on_digital === "boolean"
        ? rawOut.include_grid_on_digital
        : defaults.outputs.include_grid_on_digital,
    gpkg_tiles:
      typeof rawOut.gpkg_tiles === "boolean"
        ? rawOut.gpkg_tiles
        : defaults.outputs.gpkg_tiles,
    osmand_sqlitedb:
      typeof rawOut.osmand_sqlitedb === "boolean"
        ? rawOut.osmand_sqlitedb
        : defaults.outputs.osmand_sqlitedb,
    field_bundle_zip:
      typeof rawOut.field_bundle_zip === "boolean"
        ? rawOut.field_bundle_zip
        : defaults.outputs.field_bundle_zip,
    export_dpi:
      typeof rawOut.export_dpi === "number" && VALID_DPIS.has(rawOut.export_dpi)
        ? rawOut.export_dpi
        : defaults.outputs.export_dpi,
  };
  if (!targetState.outputs.pdf) {
    targetState.outputs.geopdf = false;
  }

  // Waypoints
  const cleanWaypoints: WaypointInput[] = [];
  if (Array.isArray(s.waypoints)) {
    for (const rawWp of s.waypoints.slice(0, MAX_SNAPSHOT_WAYPOINTS)) {
      if (!rawWp || typeof rawWp !== "object") continue;
      const w = rawWp as Record<string, unknown>;
      const lat = Number(w.lat);
      const lon = Number(w.lon);
      if (!isValidLatLon(lat, lon)) continue;
      const cat =
        typeof w.category === "string" && VALID_WP_CATEGORIES.has(w.category)
          ? (w.category as WaypointCategory)
          : "waypoint";
      const label =
        typeof w.label === "string" && w.label.trim()
          ? w.label.trim().slice(0, 40)
          : `WP${cleanWaypoints.length + 1}`;
      const wp: WaypointInput = {
        label,
        lat: Number(lat.toFixed(6)),
        lon: Number(lon.toFixed(6)),
        category: cat,
      };
      if (typeof w.notes === "string" && w.notes.trim()) {
        wp.notes = w.notes.trim().slice(0, 160);
      }
      cleanWaypoints.push(wp);
    }
  }
  targetState.waypoints = cleanWaypoints;

  // Viewsheds
  const cleanViewsheds: AppFormState["viewsheds"] = [];
  if (Array.isArray(s.viewsheds)) {
    for (const rawVs of s.viewsheds.slice(0, MAX_SNAPSHOT_VIEWSHEDS)) {
      if (!rawVs || typeof rawVs !== "object") continue;
      const v = rawVs as Record<string, unknown>;
      const lat = Number(v.lat);
      const lon = Number(v.lon);
      if (!isValidLatLon(lat, lon)) continue;
      const obs: ViewshedObserver = {
        label:
          typeof v.label === "string" && v.label.trim()
            ? v.label.trim().slice(0, 24)
            : `OBS${cleanViewsheds.length + 1}`,
        lat: Number(lat.toFixed(6)),
        lon: Number(lon.toFixed(6)),
        observer_height_m: clampFinite(Number(v.observer_height_m), 0, 500, 2),
        target_height_m: clampFinite(Number(v.target_height_m), 0, 500, 1.5),
        max_distance_m: clampFinite(
          Number(v.max_distance_m),
          250,
          25000,
          10000
        ),
      };
      cleanViewsheds.push({
        id: ids.nextViewshedId(),
        observer: obs,
        loading: false,
        error: null,
        preview: null,
      });
    }
  }
  targetState.viewsheds = cleanViewsheds;

  // RF Links
  const cleanRfLinks: AppFormState["rfLinks"] = [];
  if (Array.isArray(s.rf_links)) {
    for (const rawRf of s.rf_links.slice(0, MAX_SNAPSHOT_RF_LINKS)) {
      if (!rawRf || typeof rawRf !== "object") continue;
      const r = rawRf as Record<string, unknown>;
      const fromLat = Number(r.from_lat);
      const fromLon = Number(r.from_lon);
      const toLat = Number(r.to_lat);
      const toLon = Number(r.to_lon);
      if (!isValidLatLon(fromLat, fromLon) || !isValidLatLon(toLat, toLon)) {
        continue;
      }
      const spec: RFLinkSpec = {
        from_lat: Number(fromLat.toFixed(6)),
        from_lon: Number(fromLon.toFixed(6)),
        to_lat: Number(toLat.toFixed(6)),
        to_lon: Number(toLon.toFixed(6)),
        from_height_m: clampFinite(Number(r.from_height_m), 0, 500, 2),
        to_height_m: clampFinite(Number(r.to_height_m), 0, 500, 2),
        freq_mhz: clampFinite(Number(r.freq_mhz), 1, 100000, 915),
        k_factor: clampFinite(Number(r.k_factor), 0.5, 4, 1.333),
        from_label:
          typeof r.from_label === "string" && r.from_label.trim()
            ? r.from_label.trim().slice(0, 24)
            : `A${cleanRfLinks.length + 1}`,
        to_label:
          typeof r.to_label === "string" && r.to_label.trim()
            ? r.to_label.trim().slice(0, 24)
            : `B${cleanRfLinks.length + 1}`,
      };
      cleanRfLinks.push({
        id: ids.nextRFLinkId(),
        spec,
        loading: false,
        error: null,
        profile: null,
      });
    }
  }
  targetState.rfLinks = cleanRfLinks;

  // Tactical graphics
  const cleanTactical: TacticalGraphic[] = [];
  if (Array.isArray(s.tactical_graphics)) {
    for (const rawTac of s.tactical_graphics.slice(0, MAX_SNAPSHOT_TACTICAL)) {
      const clean = sanitizeTacticalGraphic(rawTac);
      if (clean) cleanTactical.push(clean);
    }
  }
  targetState.tacticalGraphics = cleanTactical;

  targetState.include_calibration_page = Boolean(s.include_calibration_page);
  targetState.include_route_card = Boolean(s.include_route_card);
  targetState.include_elevation_profile = Boolean(s.include_elevation_profile);
  targetState.include_atlas_index = Boolean(s.include_atlas_index);
  targetState.include_rf_link_card = Boolean(s.include_rf_link_card);
  targetState.show_fold_marks = Boolean(s.show_fold_marks);

  const basemap =
    typeof s.basemap === "string" && VALID_BASEMAPS.has(s.basemap)
      ? (s.basemap as BasemapId)
      : null;
  const zoom =
    typeof s.map_zoom === "number" && Number.isFinite(s.map_zoom)
      ? Number(clampFinite(s.map_zoom, 2, 20, 12).toFixed(2))
      : null;

  return { basemap, zoom };
}
