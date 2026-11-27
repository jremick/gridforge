import { ApiError, fetchGeocode } from "../api";
import {
  ATLAS_GRID_OPTIONS,
  AU_OVERLAY_OPTIONS,
  AU_STATE_TRAIL_CHIPS,
  CA_OVERLAY_OPTIONS,
  CARTO_TOGGLES,
  COLOR_MODE_OPTIONS,
  DEM_SOURCE_OPTIONS,
  DENSITY_OPTIONS,
  DPI_OPTIONS,
  HILLSHADE_OPTIONS,
  LOCATION_PRESETS,
  NZ_OVERLAY_OPTIONS,
  PAPER_OPTIONS,
  PRESET_REGION_TABS,
  RF_BAND_PRESETS,
  SCALE_OPTIONS,
  US_OVERLAY_OPTIONS,
  WAYPOINT_CATEGORY_OPTIONS,
  escapeHtml,
  formatBytes,
  isStripEligibleFile,
  looksLikeCoordOrMgrs,
} from "../presets";
import {
  DEFAULT_MAX_UPLOAD_BYTES,
  createDefaultNoneSession,
  formatUploadLimitLabel,
  getEffectiveMaxDpi,
  getEffectiveMaxExportDpi,
  isAtlasOptionAllowed,
  isGuestRenderPaused,
  isOutputAllowed,
} from "../session";
import { renderRFLinkSvg } from "./rfChart";
import type {
  AUOverlayKind,
  AUStateTrailKind,
  AppFormState,
  CAOverlayKind,
  ColorMode,
  DemSourceKind,
  DensityLevel,
  GarminProfile,
  GeocodeResult,
  HillshadeStrength,
  LocationPreset,
  MBTilesFormat,
  MapInteractionMode,
  NZOverlayKind,
  Orientation,
  OutputKey,
  PackSummary,
  PaperSize,
  PresetRegionTab,
  RouteMetricsResponse,
  RulerMetricsResponse,
  SessionResponse,
  USOverlayKind,
  Units,
  WaypointCategory,
} from "../types";

export interface SidebarCallbacks {
  onCenterSubmit: (centerText: string) => void;
  onPlaceSelect: (place: GeocodeResult) => void;
  onPlaceNotFound: (query: string) => void;
  onPresetSelect: (preset: LocationPreset) => void;
  onFootprintParamsChange: (options?: { fitAfterLoad?: boolean }) => void;
  onStateChange: () => void;
  onPackChange: (packId: string) => void;
  onOverlayFileChange: (file: File | null) => void;
  onAutoStripPlan: () => void;
  onClearStripPlan: () => void;
  onToggleInteractionMode: (mode: MapInteractionMode) => void;
  onWaypointEdit: (
    index: number,
    patch: { label?: string; category?: WaypointCategory }
  ) => void;
  onWaypointDelete: (index: number) => void;
  onWaypointsClear: () => void;
  onRulerPinToPdf: () => void;
  onRulerClear: () => void;
  onRingRadiiChange: (radiiKm: number[]) => void;
  onSectorFinish: () => void;
  onSectorCancel: () => void;
  onTacticalLabelEdit: (index: number, label: string) => void;
  onTacticalDelete: (index: number) => void;
  onTacticalClear: () => void;
  onViewshedEdit: (
    id: string,
    patch: {
      label?: string;
      observer_height_m?: number;
      target_height_m?: number;
      max_distance_m?: number;
    },
    recompute: boolean
  ) => void;
  onViewshedRecompute: (id: string) => void;
  onViewshedDelete: (id: string) => void;
  onViewshedsClear: () => void;
  onRFLinkUseObservers: () => void;
  onRFLinkRecompute: (id: string) => void;
  onRFLinkDelete: (id: string) => void;
  onRFLinksClear: () => void;
  onManagePacks: () => void;
  onRefreshPacks: () => void;
  onSubmitJob: () => void;
  onCopyCli: () => void;
  onApiError?: (err: unknown, source: "geocode") => void;
  onOpenAuth?: () => void;
}

const GEOCODE_DEBOUNCE_MS = 300;
const CHEVRON_SVG = `<svg class="gf-chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

const CATEGORY_COLOR_MAP: Record<WaypointCategory, string> = Object.fromEntries(
  WAYPOINT_CATEGORY_OPTIONS.map((o) => [o.value, o.color])
) as Record<WaypointCategory, string>;

const STATE_TRAIL_CHIP_SET = new Set<AUOverlayKind>(
  AU_STATE_TRAIL_CHIPS.map((c) => c.value)
);

function parseRingRadiiText(raw: string): number[] {
  const parsed = raw
    .split(/[,\s]+/)
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n) && n > 0 && n <= 100);
  return parsed.length > 0 ? parsed.slice(0, 6) : [1, 3, 5];
}

export class SidebarPanel {
  private readonly root: HTMLElement;
  private readonly state: AppFormState;
  private readonly callbacks: SidebarCallbacks;

  private centerInput!: HTMLInputElement;
  private geocodeWrapEl!: HTMLElement;
  private geocodeMenuEl!: HTMLElement;
  private presetTabsEl!: HTMLElement;
  private presetGridEl!: HTMLElement;
  private titleInput!: HTMLInputElement;
  private scaleSelect!: HTMLSelectElement;
  private paperSelect!: HTMLSelectElement;
  private atlasGridEl!: HTMLElement;
  private atlasSublabelEl!: HTMLElement;
  private atlasTierHintEl!: HTMLElement;
  private foldMarksCheck!: HTMLInputElement;
  private sheetAtlasIndexCheck!: HTMLInputElement;
  private demSelect!: HTMLSelectElement;
  private packSelect!: HTMLSelectElement;
  private managePacksBtn!: HTMLButtonElement;
  private stateAutoCheck!: HTMLInputElement;
  private contourSelect!: HTMLSelectElement;
  private hillshadeSelect!: HTMLSelectElement;
  private labelDensitySelect!: HTMLSelectElement;
  private slopeCheck!: HTMLInputElement;
  private fileInput!: HTMLInputElement;
  private fileTitleEl!: HTMLElement;
  private fileSubEl!: HTMLElement;
  private fileClearBtn!: HTMLButtonElement;
  private dropzoneEl!: HTMLElement;
  private stripBarEl!: HTMLElement;

  // Section 05 Waypoints & Tactical Markup
  private waypointsSectionEl!: HTMLDetailsElement;
  private waypointsPillEl!: HTMLElement;
  private wpClearBtn!: HTMLButtonElement;
  private wpRouteCardCheck!: HTMLInputElement;
  private wpListEl!: HTMLElement;
  private wpTotalEl!: HTMLElement;
  private rulerBoxEl!: HTMLElement;
  private ringRadiiInput!: HTMLInputElement;
  private sectorDraftBarEl!: HTMLElement;
  private tacticalListEl!: HTMLElement;
  private tacticalClearBtn!: HTMLButtonElement;

  // Section 06 RF / Viewshed
  private viewshedSectionEl!: HTMLDetailsElement;
  private viewshedPillEl!: HTMLElement;
  private vsToggleBtn!: HTMLButtonElement;
  private vsClearBtn!: HTMLButtonElement;
  private vsDefObsInput!: HTMLInputElement;
  private vsDefTgtInput!: HTMLInputElement;
  private vsDefRadInput!: HTMLInputElement;
  private vsListEl!: HTMLElement;
  private rfPickBtn!: HTMLButtonElement;
  private rfUseObsBtn!: HTMLButtonElement;
  private rfClearBtn!: HTMLButtonElement;
  private rfBandSelect!: HTMLSelectElement;
  private rfFreqInput!: HTMLInputElement;
  private rfTxHInput!: HTMLInputElement;
  private rfRxHInput!: HTMLInputElement;
  private rfCardCheck!: HTMLInputElement;
  private rfLinksListEl!: HTMLElement;

  // Sticky Footer Outputs
  private outPdfCheck!: HTMLInputElement;
  private outPdfDpiSelect!: HTMLSelectElement;
  private outGeoPdfCheck!: HTMLInputElement;
  private outMbtilesCheck!: HTMLInputElement;
  private outMbtilesFmt!: HTMLSelectElement;
  private outKmzCheck!: HTMLInputElement;
  private outGarminProfile!: HTMLSelectElement;
  private outGpkgCheck!: HTMLInputElement;
  private outOsmandCheck!: HTMLInputElement;
  private outBundleCheck!: HTMLInputElement;
  private outDigitalGridCheck!: HTMLInputElement;
  private outExportDpiSelect!: HTMLSelectElement;
  private digitalGridRow!: HTMLElement;
  private suppPagesBoxEl!: HTMLElement;
  private outCalibrationCheck!: HTMLInputElement;
  private outRouteCardCheck!: HTMLInputElement;
  private outElevProfileCheck!: HTMLInputElement;
  private outAtlasIndexCheck!: HTMLInputElement;
  private outRfLinkCardCheck!: HTMLInputElement;
  private turnstileSlotEl!: HTMLElement;
  private quotaNoticeEl!: HTMLElement;
  private generateBtn!: HTMLButtonElement;
  private formErrorEl!: HTMLElement;
  private sheetPillEl!: HTMLElement;
  private overlayPillEl!: HTMLElement;
  private cartoPillEl!: HTMLElement;

  private session: SessionResponse = createDefaultNoneSession();
  private maxUploadBytes = DEFAULT_MAX_UPLOAD_BYTES;
  private isSubmitting = false;
  private activePresetTab: PresetRegionTab = "nsw-vic";
  private activePresetId: string | null = "blue-mountains";
  private interactionMode: MapInteractionMode = "pan";
  private planningStrip = false;
  private routeMetrics: RouteMetricsResponse | null = null;
  private rulerPoints: { lat: number; lon: number }[] = [];
  private rulerMetrics: RulerMetricsResponse | null = null;
  private rulerLoading = false;
  private sectorDraftCount = 0;
  private rfPendingFrom = false;

  private geocodeTimer: number | null = null;
  private geocodeAbort: AbortController | null = null;
  private geocodeResults: GeocodeResult[] = [];
  private geocodeHighlightIdx = -1;

  constructor(
    container: HTMLElement,
    state: AppFormState,
    callbacks: SidebarCallbacks,
    initialPresetId: string | null = "blue-mountains"
  ) {
    this.root = container;
    this.state = state;
    this.callbacks = callbacks;
    this.activePresetId = initialPresetId;
    this.root.className = "gf-sidebar";
    this.render();
    this.bindEvents();
    this.setPacks([]);
    this.syncControlsFromState();
  }

  private render(): void {
    const presetTabsHtml = PRESET_REGION_TABS.map(
      (t) =>
        `<button type="button" class="gf-seg-btn ${t.id === this.activePresetTab ? "active" : ""}" data-preset-tab="${t.id}">${escapeHtml(t.label)}</button>`
    ).join("");

    const scaleOptsHtml = SCALE_OPTIONS.map(
      (s) => `<option value="${s.value}">${escapeHtml(s.label)}</option>`
    ).join("");

    const paperOptsHtml = PAPER_OPTIONS.map(
      (p) =>
        `<option value="${p.value}">${escapeHtml(p.label)} (${escapeHtml(p.dims)})</option>`
    ).join("");

    const atlasBtnsHtml = ATLAS_GRID_OPTIONS.map(
      (a) =>
        `<button type="button" class="gf-atlas-btn" data-atlas-cols="${a.cols}" data-atlas-rows="${a.rows}" title="${a.cols * a.rows === 1 ? "Single sheet" : `${a.cols * a.rows} edge-matched sheets`}">${escapeHtml(a.label)}</button>`
    ).join("");

    const themeBtnsHtml = COLOR_MODE_OPTIONS.map(
      (m) => `
        <button type="button" class="gf-theme-btn" data-value="${m.value}" title="${escapeHtml(m.hint)}">
          <span class="gf-theme-swatch" style="background:${m.swatch}"></span>
          <span>${escapeHtml(m.label)}</span>
        </button>
      `
    ).join("");

    const demOptsHtml = DEM_SOURCE_OPTIONS.map(
      (d) => `<option value="${d.value}">${escapeHtml(d.label)}</option>`
    ).join("");

    const stateChipsHtml = AU_STATE_TRAIL_CHIPS.map(
      (c) =>
        `<button type="button" class="gf-state-chip" data-au-state-chip="${c.value}" aria-pressed="false" title="Pin ${escapeHtml(c.name)} trails, huts &amp; trigs">${escapeHtml(c.label)}</button>`
    ).join("");

    const auOverlaysHtml = AU_OVERLAY_OPTIONS.map(
      (o) => `
        <label class="gf-check-row">
          <input type="checkbox" data-au-overlay="${o.value}" />
          <span class="gf-check-text">
            <span class="gf-check-title">${escapeHtml(o.label)}</span>
            <span class="gf-check-hint">${escapeHtml(o.hint)}</span>
          </span>
        </label>
      `
    ).join("");

    const nzOverlaysHtml = NZ_OVERLAY_OPTIONS.map(
      (o) => `
        <label class="gf-check-row">
          <input type="checkbox" data-nz-overlay="${o.value}" />
          <span class="gf-check-text">
            <span class="gf-check-title">${escapeHtml(o.label)}</span>
            <span class="gf-check-hint">${escapeHtml(o.hint)}</span>
          </span>
        </label>
      `
    ).join("");

    const caOverlaysHtml = CA_OVERLAY_OPTIONS.map(
      (o) => `
        <label class="gf-check-row">
          <input type="checkbox" data-ca-overlay="${o.value}" />
          <span class="gf-check-text">
            <span class="gf-check-title">${escapeHtml(o.label)}</span>
            <span class="gf-check-hint">${escapeHtml(o.hint)}</span>
          </span>
        </label>
      `
    ).join("");

    const usOverlaysHtml = US_OVERLAY_OPTIONS.map(
      (o) => `
        <label class="gf-check-row">
          <input type="checkbox" data-us-overlay="${o.value}" />
          <span class="gf-check-text">
            <span class="gf-check-title">${escapeHtml(o.label)}</span>
            <span class="gf-check-hint">${escapeHtml(o.hint)}</span>
          </span>
        </label>
      `
    ).join("");

    const densityOptsHtml = DENSITY_OPTIONS.map(
      (d) => `<option value="${d.value}">${escapeHtml(d.label)}</option>`
    ).join("");

    const hillshadeOptsHtml = HILLSHADE_OPTIONS.map(
      (h) => `<option value="${h.value}">${escapeHtml(h.label)}</option>`
    ).join("");

    const dpiOptsHtml = DPI_OPTIONS.map(
      (d) => `<option value="${d.value}">${escapeHtml(d.shortLabel)}</option>`
    ).join("");

    const cartoTogglesHtml = CARTO_TOGGLES.map(
      (t) => `
        <label class="gf-toggle-chip">
          <input type="checkbox" data-carto-toggle="${t.key}" />
          <span>${escapeHtml(t.label)}</span>
        </label>
      `
    ).join("");

    const rfBandOptsHtml = [
      ...RF_BAND_PRESETS.map(
        (b) =>
          `<option value="${b.freq_mhz}">${escapeHtml(b.label)} (${b.freq_mhz} MHz)</option>`
      ),
      `<option value="custom">Custom MHz</option>`,
    ].join("");

    this.root.innerHTML = `
      <div class="gf-sidebar-scroll">
        <!-- 1. Location & Presets -->
        <details class="gf-section" open>
          <summary class="gf-section-summary">
            <span class="gf-section-title-group">
              <span class="gf-section-index">01</span>
              <span class="gf-section-title">Location &amp; Presets</span>
            </span>
            <span class="gf-section-meta">
              ${CHEVRON_SVG}
            </span>
          </summary>
          <div class="gf-section-body">
            <div class="gf-field">
              <label class="gf-label" for="gf-center-input">
                <span>Coordinate, MGRS, or Place Name</span>
                <span class="gf-sublabel">/ to search</span>
              </label>
              <div class="gf-geocode-wrap" data-role="geocode-wrap">
                <div class="gf-input-group">
                  <input
                    id="gf-center-input"
                    class="gf-input mono"
                    type="text"
                    placeholder="Kosciuszko, -33.732, 150.312, or 56HLH…"
                    autocomplete="off"
                    spellcheck="false"
                  />
                  <button type="button" class="gf-btn-secondary" data-role="go-center">Go</button>
                </div>
                <div class="gf-geocode-menu hidden" data-role="geocode-menu" role="listbox"></div>
              </div>
            </div>
            <div class="gf-field">
              <div class="gf-label">
                <span>Quick-Jump Presets</span>
                <span class="gf-sublabel">32 curated regions</span>
              </div>
              <div class="gf-segmented gf-preset-tabs" data-role="preset-tabs">
                ${presetTabsHtml}
              </div>
              <div class="gf-preset-grid" data-role="preset-grid"></div>
            </div>
          </div>
        </details>

        <!-- 2. Sheet & Print Settings -->
        <details class="gf-section" open>
          <summary class="gf-section-summary">
            <span class="gf-section-title-group">
              <span class="gf-section-index">02</span>
              <span class="gf-section-title">Sheet &amp; Print</span>
            </span>
            <span class="gf-section-meta">
              <span class="gf-section-pill" data-role="sheet-pill">1:25k · A4 P</span>
              ${CHEVRON_SVG}
            </span>
          </summary>
          <div class="gf-section-body">
            <div class="gf-field">
              <label class="gf-label" for="gf-title-input">
                <span>Sheet Title</span>
                <span class="gf-sublabel">Optional collar header</span>
              </label>
              <input
                id="gf-title-input"
                class="gf-input"
                type="text"
                maxlength="80"
                placeholder="Auto from MGRS / location"
              />
            </div>

            <div class="gf-field-row">
              <div class="gf-field">
                <label class="gf-label" for="gf-scale-select">Scale</label>
                <select id="gf-scale-select" class="gf-select mono">
                  ${scaleOptsHtml}
                </select>
              </div>
              <div class="gf-field">
                <label class="gf-label" for="gf-paper-select">Paper</label>
                <select id="gf-paper-select" class="gf-select">
                  ${paperOptsHtml}
                </select>
              </div>
            </div>

            <div class="gf-field">
              <span class="gf-label">
                <span>Atlas Grid</span>
                <span class="gf-sublabel" data-role="atlas-sublabel">Single sheet</span>
              </span>
              <div class="gf-atlas-grid" data-role="atlas-grid">
                ${atlasBtnsHtml}
              </div>
              <div class="gf-tier-hint hidden" data-role="atlas-tier-hint"></div>
              <div class="gf-atlas-extras">
                <label class="gf-toggle-chip" title="Draw 0.20 mm corner registration & fold/join marks for multi-sheet assembly">
                  <input type="checkbox" data-role="fold-marks" />
                  <span>Fold / Join Marks</span>
                </label>
                <label class="gf-toggle-chip" title="Prepend pure-vector Atlas Overview & Sheet Index cover page">
                  <input type="checkbox" data-role="sheet-atlas-index" />
                  <span>Atlas Index Cover</span>
                </label>
              </div>
            </div>

            <div class="gf-field">
              <span class="gf-label">
                <span>Map Theme</span>
                <span class="gf-sublabel">Color · B&amp;W · Night-Red · Alpine</span>
              </span>
              <div class="gf-theme-grid" data-seg="color_mode">
                ${themeBtnsHtml}
              </div>
            </div>

            <div class="gf-field-row">
              <div class="gf-field">
                <span class="gf-label">Orientation</span>
                <div class="gf-segmented" data-seg="orientation">
                  <button type="button" class="gf-seg-btn" data-value="portrait">Portrait</button>
                  <button type="button" class="gf-seg-btn" data-value="landscape">Landscape</button>
                </div>
              </div>
              <div class="gf-field">
                <span class="gf-label">Units</span>
                <div class="gf-segmented" data-seg="units">
                  <button type="button" class="gf-seg-btn" data-value="metric">Metric</button>
                  <button type="button" class="gf-seg-btn" data-value="imperial">Imperial</button>
                </div>
              </div>
            </div>
          </div>
        </details>

        <!-- 3. Elevation & Regional Overlays -->
        <details class="gf-section" open>
          <summary class="gf-section-summary">
            <span class="gf-section-title-group">
              <span class="gf-section-index">03</span>
              <span class="gf-section-title">Elevation &amp; Overlays</span>
            </span>
            <span class="gf-section-meta">
              <span class="gf-section-pill" data-role="overlay-pill">Auto</span>
              ${CHEVRON_SVG}
            </span>
          </summary>
          <div class="gf-section-body">
            <div class="gf-field">
              <label class="gf-label" for="gf-dem-select">DEM Elevation Source</label>
              <select id="gf-dem-select" class="gf-select">
                ${demOptsHtml}
              </select>
            </div>

            <div class="gf-field">
              <label class="gf-label" for="gf-pack-select">
                <span>Offline Pack</span>
                <span class="gf-label-actions">
                  <button type="button" class="gf-mini-btn gf-manage-packs-btn" data-role="manage-packs" title="Build or manage air-gapped offline packs">Manage Packs</button>
                  <button type="button" class="gf-mini-btn" data-role="refresh-packs" title="Reload installed packs">↻</button>
                </span>
              </label>
              <select id="gf-pack-select" class="gf-select">
                <option value="">Cloud-Native On-Demand</option>
              </select>
            </div>

            <div class="gf-overlay-group">
              <span class="gf-overlay-heading">Australia Overlays (CC BY 4.0)</span>
              <div class="gf-state-trails-box">
                <label class="gf-check-row gf-state-auto-row">
                  <input type="checkbox" data-role="au-state-auto" />
                  <span class="gf-check-text">
                    <span class="gf-check-title">State Fire Trails, Huts &amp; Trigs (Auto — All 8 States)</span>
                    <span class="gf-check-hint">Auto-detects NSW, VIC, TAS, QLD, WA, SA, NT, ACT (incl. cross-border)</span>
                  </span>
                </label>
                <div class="gf-state-chip-bar" role="group" aria-label="Override specific Australian states">
                  ${stateChipsHtml}
                </div>
              </div>
              ${auOverlaysHtml}
            </div>

            <div class="gf-overlay-group">
              <span class="gf-overlay-heading">New Zealand Overlays (CC BY 4.0)</span>
              ${nzOverlaysHtml}
            </div>

            <div class="gf-overlay-group">
              <span class="gf-overlay-heading">North America Overlays (US &amp; Canada)</span>
              ${usOverlaysHtml}
              ${caOverlaysHtml}
            </div>
          </div>
        </details>

        <!-- 4. Cartography & User Overlay -->
        <details class="gf-section" data-role="carto-section">
          <summary class="gf-section-summary">
            <span class="gf-section-title-group">
              <span class="gf-section-index">04</span>
              <span class="gf-section-title">Cartography &amp; File Overlay</span>
            </span>
            <span class="gf-section-meta">
              <span class="gf-section-pill" data-role="carto-pill">Default</span>
              ${CHEVRON_SVG}
            </span>
          </summary>
          <div class="gf-section-body">
            <div class="gf-field-row-3">
              <div class="gf-field">
                <label class="gf-label" for="gf-contour-select">Contours</label>
                <select id="gf-contour-select" class="gf-select">
                  ${densityOptsHtml}
                </select>
              </div>
              <div class="gf-field">
                <label class="gf-label" for="gf-hillshade-select">Hillshade</label>
                <select id="gf-hillshade-select" class="gf-select">
                  ${hillshadeOptsHtml}
                </select>
              </div>
              <div class="gf-field">
                <label class="gf-label" for="gf-label-select">Labels</label>
                <select id="gf-label-select" class="gf-select">
                  ${densityOptsHtml}
                </select>
              </div>
            </div>

            <div class="gf-field">
              <span class="gf-label">Map Layer Toggles</span>
              <div class="gf-toggle-grid">
                ${cartoTogglesHtml}
              </div>
              <label class="gf-slope-toggle">
                <span class="gf-slope-left">
                  <input type="checkbox" data-role="slope-shading" />
                  <span>Slope-Angle Shading (&gt;30° Avalanche)</span>
                </span>
                <span class="gf-slope-legend" title="Slope bands: 27–30° yellow, 30–35° orange, 35–45° red, 45°+ purple">
                  <i style="background:#F6E05E"></i>
                  <i style="background:#ED8936"></i>
                  <i style="background:#E53E3E"></i>
                  <i style="background:#805AD5"></i>
                </span>
              </label>
            </div>

            <div class="gf-field">
              <span class="gf-label">
                <span>Custom User Overlay &amp; Strip Route</span>
                <span class="gf-sublabel">.geojson · .gpx · .csv</span>
              </span>
              <div class="gf-dropzone" data-role="dropzone">
                <label class="gf-dropzone-label" for="gf-overlay-file">
                  <span class="gf-dropzone-title" data-role="file-title">Drop or browse overlay file…</span>
                  <span class="gf-dropzone-sub" data-role="file-sub">Waypoints, tracks, or polygons (max ${escapeHtml(formatUploadLimitLabel(this.maxUploadBytes))})</span>
                </label>
                <input
                  id="gf-overlay-file"
                  type="file"
                  accept=".geojson,.json,.gpx,.csv"
                  hidden
                />
                <button type="button" class="gf-file-clear" data-role="file-clear" title="Clear overlay file" hidden>×</button>
              </div>
              <div class="gf-strip-bar hidden" data-role="strip-bar"></div>
            </div>
          </div>
        </details>

        <!-- 5. Waypoints & Tactical Markup -->
        <details class="gf-section" data-role="waypoints-section">
          <summary class="gf-section-summary">
            <span class="gf-section-title-group">
              <span class="gf-section-index">05</span>
              <span class="gf-section-title">Waypoints &amp; Tactical</span>
            </span>
            <span class="gf-section-meta">
              <span class="gf-section-pill" data-role="waypoints-pill">0 WPs</span>
              ${CHEVRON_SVG}
            </span>
          </summary>
          <div class="gf-section-body">
            <div class="gf-tactical-tools-grid">
              <button type="button" class="gf-tool-btn" data-tool-btn="markup" title="Drop numbered waypoints on map (M)">⌖ WP</button>
              <button type="button" class="gf-tool-btn" data-tool-btn="ruler" title="Measure distance, magnetic bearing & grade (R)">📏 Ruler</button>
              <button type="button" class="gf-tool-btn" data-tool-btn="ring" title="Drop concentric range rings on map">◎ Ring</button>
              <button type="button" class="gf-tool-btn" data-tool-btn="sector" title="Click ≥3 vertices to draw shaded AOI sector">⬠ Sector</button>
            </div>

            <div class="gf-ruler-box hidden" data-role="ruler-box"></div>
            <div class="gf-sector-draft-bar hidden" data-role="sector-draft-bar"></div>

            <div class="gf-field-row gf-ring-opt-row">
              <div class="gf-field">
                <label class="gf-label" for="gf-ring-radii">Ring Radii (km)</label>
                <input
                  id="gf-ring-radii"
                  class="gf-input mono"
                  type="text"
                  data-role="ring-radii-input"
                  value="1, 3, 5"
                  placeholder="1, 3, 5"
                />
              </div>
              <div class="gf-field gf-tactical-clear-wrap">
                <span class="gf-label">Graphics</span>
                <button type="button" class="gf-mini-btn danger" data-role="tactical-clear-all" disabled>Clear Graphics</button>
              </div>
            </div>

            <div class="gf-wp-list" data-role="tactical-list"></div>

            <div class="gf-subdivider">
              <span>Route Waypoints</span>
              <button type="button" class="gf-mini-btn danger" data-role="wp-clear-all" disabled>Clear WPs</button>
            </div>

            <label class="gf-inline-check gf-wp-card-toggle">
              <input type="checkbox" data-role="wp-route-card" />
              <span>Include Tactical Route Card Page (MGRS &amp; Mag Azimuths)</span>
            </label>

            <div class="gf-wp-list" data-role="wp-list"></div>
            <div class="gf-wp-total hidden" data-role="wp-total"></div>
          </div>
        </details>

        <!-- 6. RF / Line-of-Sight Viewshed & Link Profile -->
        <details class="gf-section" data-role="viewshed-section">
          <summary class="gf-section-summary">
            <span class="gf-section-title-group">
              <span class="gf-section-index">06</span>
              <span class="gf-section-title">RF / Viewshed</span>
            </span>
            <span class="gf-section-meta">
              <span class="gf-section-pill" data-role="viewshed-pill">Off</span>
              ${CHEVRON_SVG}
            </span>
          </summary>
          <div class="gf-section-body">
            <div class="gf-subdivider"><span>Area Viewshed (DEM LOS)</span></div>
            <div class="gf-wp-controls">
              <button type="button" class="gf-wp-markup-btn gf-vs-toggle-btn" data-role="vs-toggle">
                <span>📡 Drop Observer</span>
              </button>
              <button type="button" class="gf-mini-btn danger" data-role="vs-clear-all" disabled>
                Clear All
              </button>
            </div>

            <div class="gf-field-row-3">
              <div class="gf-field">
                <label class="gf-label" for="gf-vs-obs-h">Obs H (m)</label>
                <input id="gf-vs-obs-h" class="gf-input mono" type="number" min="0" max="500" step="0.5" value="2" />
              </div>
              <div class="gf-field">
                <label class="gf-label" for="gf-vs-tgt-h">Tgt H (m)</label>
                <input id="gf-vs-tgt-h" class="gf-input mono" type="number" min="0" max="500" step="0.5" value="1.5" />
              </div>
              <div class="gf-field">
                <label class="gf-label" for="gf-vs-rad-km">Max (km)</label>
                <input id="gf-vs-rad-km" class="gf-input mono" type="number" min="0.25" max="25" step="0.5" value="10" />
              </div>
            </div>

            <div class="gf-wp-list" data-role="vs-list"></div>

            <!-- Point-to-Point RF Link Profile (A ↔ B) -->
            <div class="gf-subdivider">
              <span>Point-to-Point RF Link (A ↔ B)</span>
              <button type="button" class="gf-mini-btn danger" data-role="rf-link-clear" disabled>Clear</button>
            </div>

            <div class="gf-wp-controls">
              <button type="button" class="gf-wp-markup-btn gf-rf-pick-btn" data-role="rf-link-toggle">
                <span>⚡ A ↔ B Pick</span>
              </button>
              <button type="button" class="gf-mini-btn" data-role="rf-use-obs" disabled title="Compute Fresnel link between OBS1 and OBS2">
                OBS1 → OBS2
              </button>
            </div>

            <div class="gf-field">
              <label class="gf-label" for="gf-rf-band">Radio Band / Frequency</label>
              <select id="gf-rf-band" class="gf-select" data-role="rf-band">
                ${rfBandOptsHtml}
              </select>
            </div>

            <div class="gf-field-row-3">
              <div class="gf-field">
                <label class="gf-label" for="gf-rf-freq">Freq (MHz)</label>
                <input id="gf-rf-freq" class="gf-input mono" type="number" min="30" max="60000" step="0.1" value="915" />
              </div>
              <div class="gf-field">
                <label class="gf-label" for="gf-rf-tx-h">Tx H (m)</label>
                <input id="gf-rf-tx-h" class="gf-input mono" type="number" min="0" max="500" step="0.5" value="2" />
              </div>
              <div class="gf-field">
                <label class="gf-label" for="gf-rf-rx-h">Rx H (m)</label>
                <input id="gf-rf-rx-h" class="gf-input mono" type="number" min="0" max="500" step="0.5" value="2" />
              </div>
            </div>

            <label class="gf-inline-check">
              <input type="checkbox" data-role="rf-link-card" />
              <span>Include RF Fresnel Link Card PDF Page</span>
            </label>

            <div class="gf-wp-list gf-rf-list" data-role="rf-links-list"></div>
          </div>
        </details>
      </div>

      <!-- Sticky Outputs & Generate Footer -->
      <div class="gf-sidebar-footer">
        <div class="gf-outputs-box">
          <div class="gf-output-line">
            <label class="gf-output-check">
              <input type="checkbox" data-role="out-pdf" />
              <span>Print PDF</span>
            </label>
            <div class="gf-output-inline">
              <select
                class="gf-mini-select"
                data-role="out-pdf-dpi"
                aria-label="Print PDF resolution (DPI)"
                title="Print PDF resolution (300 DPI standard; 450/600 DPI ultra-sharp hillshade)"
              >
                ${dpiOptsHtml}
              </select>
              <label class="gf-inline-check" title="Embed geospatial reference tags in PDF">
                <input type="checkbox" data-role="out-geopdf" />
                <span>GeoPDF</span>
              </label>
            </div>
          </div>

          <div class="gf-supp-pages" data-role="supp-pages">
            <label class="gf-inline-check" title="Append true-scale 100 mm ruler & MGRS Romer verification page">
              <input type="checkbox" data-role="out-calibration" />
              <span>100 mm Scale Calibration &amp; Romer Page</span>
            </label>
            <div class="gf-supp-chips">
              <label class="gf-inline-check" title="Append Tactical Waypoints & Magnetic Bearings table page">
                <input type="checkbox" data-role="out-route-card" />
                <span>Route Card</span>
              </label>
              <label class="gf-inline-check" title="Prepend DEM Elevation Profile cover page">
                <input type="checkbox" data-role="out-elev-profile" />
                <span>Elev Profile</span>
              </label>
              <label class="gf-inline-check" title="Prepend Atlas Overview & Sheet Index cover page">
                <input type="checkbox" data-role="out-atlas-index" />
                <span>Atlas Index</span>
              </label>
              <label class="gf-inline-check" title="Append RF Fresnel Zone & FSPL Link Card PDF page">
                <input type="checkbox" data-role="out-rf-link-card" />
                <span>RF Link Card</span>
              </label>
            </div>
          </div>

          <div class="gf-output-line">
            <label class="gf-output-check">
              <input type="checkbox" data-role="out-mbtiles" />
              <span>ATAK .mbtiles</span>
            </label>
            <div class="gf-output-inline">
              <select class="gf-mini-select" data-role="out-mbtiles-fmt" aria-label="MBTiles tile format">
                <option value="png">PNG</option>
                <option value="jpg">JPG</option>
              </select>
            </div>
          </div>

          <div class="gf-output-line">
            <label class="gf-output-check">
              <input type="checkbox" data-role="out-kmz" />
              <span>Garmin .kmz</span>
            </label>
            <div class="gf-output-inline">
              <select class="gf-mini-select" data-role="out-garmin-profile" aria-label="Garmin Custom Maps profile">
                <option value="standard">Standard ≤100</option>
                <option value="modern">Modern ≤500</option>
              </select>
            </div>
          </div>

          <div class="gf-extra-outputs">
            <label class="gf-output-chip" title="OGC GeoPackage Raster Tiles (.gpkg) for QGIS, AlpineQuest & Gaia">
              <input type="checkbox" data-role="out-gpkg" />
              <span>.gpkg</span>
            </label>
            <label class="gf-output-chip" title="OsmAnd / Locus Map BigPlanet SQLite (.sqlitedb)">
              <input type="checkbox" data-role="out-osmand" />
              <span>.sqlitedb</span>
            </label>
            <label class="gf-output-chip gf-bundle-chip" title="Single Field Bundle (.zip) containing PDF, digital tiles, GPX 1.1 & README">
              <input type="checkbox" data-role="out-bundle" />
              <span>Field Bundle (.zip)</span>
            </label>
          </div>

          <div class="gf-digital-opts-row" data-role="digital-grid-row">
            <label class="gf-inline-check">
              <input type="checkbox" data-role="out-digital-grid" />
              <span>MGRS Grid on Digital</span>
            </label>
            <div class="gf-output-inline">
              <span class="gf-mini-label">Tiles</span>
              <select
                class="gf-mini-select"
                data-role="out-export-dpi"
                aria-label="Digital export tile resolution (DPI)"
                title="Digital export rasterization DPI (higher DPI generates sharper max zoom tiles)"
              >
                ${dpiOptsHtml}
              </select>
            </div>
          </div>
        </div>

        <div class="gf-turnstile-slot hidden" data-role="turnstile-slot"></div>
        <div class="gf-quota-notice hidden" data-role="quota-notice" role="status"></div>
        <div class="gf-form-error hidden" data-role="form-error"></div>

        <div class="gf-footer-actions">
          <button type="button" class="gf-generate-btn" data-role="generate-btn">
            <span>Generate Map Sheet</span>
          </button>
          <button
            type="button"
            class="gf-cli-btn"
            data-role="copy-cli"
            aria-label="Copy CLI command"
            title="Copy Docker Compose command"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M2.5 4.5L6 8L2.5 11.5M7.5 11.5H13.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <span>CLI</span>
          </button>
        </div>
      </div>
    `;

    this.centerInput = this.q<HTMLInputElement>("#gf-center-input");
    this.geocodeWrapEl = this.q("[data-role='geocode-wrap']");
    this.geocodeMenuEl = this.q("[data-role='geocode-menu']");
    this.presetTabsEl = this.q("[data-role='preset-tabs']");
    this.presetGridEl = this.q("[data-role='preset-grid']");
    this.titleInput = this.q<HTMLInputElement>("#gf-title-input");
    this.scaleSelect = this.q<HTMLSelectElement>("#gf-scale-select");
    this.paperSelect = this.q<HTMLSelectElement>("#gf-paper-select");
    this.atlasGridEl = this.q("[data-role='atlas-grid']");
    this.atlasSublabelEl = this.q("[data-role='atlas-sublabel']");
    this.atlasTierHintEl = this.q("[data-role='atlas-tier-hint']");
    this.foldMarksCheck = this.q<HTMLInputElement>("[data-role='fold-marks']");
    this.sheetAtlasIndexCheck = this.q<HTMLInputElement>(
      "[data-role='sheet-atlas-index']"
    );
    this.demSelect = this.q<HTMLSelectElement>("#gf-dem-select");
    this.packSelect = this.q<HTMLSelectElement>("#gf-pack-select");
    this.managePacksBtn = this.q<HTMLButtonElement>("[data-role='manage-packs']");
    this.stateAutoCheck = this.q<HTMLInputElement>("[data-role='au-state-auto']");
    this.contourSelect = this.q<HTMLSelectElement>("#gf-contour-select");
    this.hillshadeSelect = this.q<HTMLSelectElement>("#gf-hillshade-select");
    this.labelDensitySelect = this.q<HTMLSelectElement>("#gf-label-select");
    this.slopeCheck = this.q<HTMLInputElement>("[data-role='slope-shading']");
    this.fileInput = this.q<HTMLInputElement>("#gf-overlay-file");
    this.fileTitleEl = this.q("[data-role='file-title']");
    this.fileSubEl = this.q("[data-role='file-sub']");
    this.fileClearBtn = this.q<HTMLButtonElement>("[data-role='file-clear']");
    this.dropzoneEl = this.q("[data-role='dropzone']");
    this.stripBarEl = this.q("[data-role='strip-bar']");

    this.waypointsSectionEl = this.q<HTMLDetailsElement>(
      "[data-role='waypoints-section']"
    );
    this.waypointsPillEl = this.q("[data-role='waypoints-pill']");
    this.wpClearBtn = this.q<HTMLButtonElement>("[data-role='wp-clear-all']");
    this.wpRouteCardCheck = this.q<HTMLInputElement>(
      "[data-role='wp-route-card']"
    );
    this.wpListEl = this.q("[data-role='wp-list']");
    this.wpTotalEl = this.q("[data-role='wp-total']");
    this.rulerBoxEl = this.q("[data-role='ruler-box']");
    this.ringRadiiInput = this.q<HTMLInputElement>(
      "[data-role='ring-radii-input']"
    );
    this.sectorDraftBarEl = this.q("[data-role='sector-draft-bar']");
    this.tacticalListEl = this.q("[data-role='tactical-list']");
    this.tacticalClearBtn = this.q<HTMLButtonElement>(
      "[data-role='tactical-clear-all']"
    );

    this.viewshedSectionEl = this.q<HTMLDetailsElement>(
      "[data-role='viewshed-section']"
    );
    this.viewshedPillEl = this.q("[data-role='viewshed-pill']");
    this.vsToggleBtn = this.q<HTMLButtonElement>("[data-role='vs-toggle']");
    this.vsClearBtn = this.q<HTMLButtonElement>("[data-role='vs-clear-all']");
    this.vsDefObsInput = this.q<HTMLInputElement>("#gf-vs-obs-h");
    this.vsDefTgtInput = this.q<HTMLInputElement>("#gf-vs-tgt-h");
    this.vsDefRadInput = this.q<HTMLInputElement>("#gf-vs-rad-km");
    this.vsListEl = this.q("[data-role='vs-list']");

    this.rfPickBtn = this.q<HTMLButtonElement>("[data-role='rf-link-toggle']");
    this.rfUseObsBtn = this.q<HTMLButtonElement>("[data-role='rf-use-obs']");
    this.rfClearBtn = this.q<HTMLButtonElement>("[data-role='rf-link-clear']");
    this.rfBandSelect = this.q<HTMLSelectElement>("[data-role='rf-band']");
    this.rfFreqInput = this.q<HTMLInputElement>("#gf-rf-freq");
    this.rfTxHInput = this.q<HTMLInputElement>("#gf-rf-tx-h");
    this.rfRxHInput = this.q<HTMLInputElement>("#gf-rf-rx-h");
    this.rfCardCheck = this.q<HTMLInputElement>("[data-role='rf-link-card']");
    this.rfLinksListEl = this.q("[data-role='rf-links-list']");

    this.outPdfCheck = this.q<HTMLInputElement>("[data-role='out-pdf']");
    this.outPdfDpiSelect = this.q<HTMLSelectElement>("[data-role='out-pdf-dpi']");
    this.outGeoPdfCheck = this.q<HTMLInputElement>("[data-role='out-geopdf']");
    this.outMbtilesCheck = this.q<HTMLInputElement>("[data-role='out-mbtiles']");
    this.outMbtilesFmt = this.q<HTMLSelectElement>(
      "[data-role='out-mbtiles-fmt']"
    );
    this.outKmzCheck = this.q<HTMLInputElement>("[data-role='out-kmz']");
    this.outGarminProfile = this.q<HTMLSelectElement>(
      "[data-role='out-garmin-profile']"
    );
    this.outGpkgCheck = this.q<HTMLInputElement>("[data-role='out-gpkg']");
    this.outOsmandCheck = this.q<HTMLInputElement>("[data-role='out-osmand']");
    this.outBundleCheck = this.q<HTMLInputElement>("[data-role='out-bundle']");
    this.outDigitalGridCheck = this.q<HTMLInputElement>(
      "[data-role='out-digital-grid']"
    );
    this.outExportDpiSelect = this.q<HTMLSelectElement>(
      "[data-role='out-export-dpi']"
    );
    this.digitalGridRow = this.q("[data-role='digital-grid-row']");
    this.suppPagesBoxEl = this.q("[data-role='supp-pages']");
    this.outCalibrationCheck = this.q<HTMLInputElement>(
      "[data-role='out-calibration']"
    );
    this.outRouteCardCheck = this.q<HTMLInputElement>(
      "[data-role='out-route-card']"
    );
    this.outElevProfileCheck = this.q<HTMLInputElement>(
      "[data-role='out-elev-profile']"
    );
    this.outAtlasIndexCheck = this.q<HTMLInputElement>(
      "[data-role='out-atlas-index']"
    );
    this.outRfLinkCardCheck = this.q<HTMLInputElement>(
      "[data-role='out-rf-link-card']"
    );
    this.turnstileSlotEl = this.q("[data-role='turnstile-slot']");
    this.quotaNoticeEl = this.q("[data-role='quota-notice']");
    this.generateBtn = this.q<HTMLButtonElement>("[data-role='generate-btn']");
    this.formErrorEl = this.q("[data-role='form-error']");
    this.sheetPillEl = this.q("[data-role='sheet-pill']");
    this.overlayPillEl = this.q("[data-role='overlay-pill']");
    this.cartoPillEl = this.q("[data-role='carto-pill']");

    this.renderPresetCards();
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Sidebar element not found: ${selector}`);
    return el;
  }

  private renderPresetCards(): void {
    const list = LOCATION_PRESETS.filter((p) => p.tab === this.activePresetTab);
    this.presetGridEl.innerHTML = list
      .map(
        (p) => `
          <button type="button" class="gf-preset-btn ${p.id === this.activePresetId ? "active" : ""}" data-preset="${escapeHtml(p.id)}">
            <span class="gf-preset-name">${escapeHtml(p.label)}</span>
            <span class="gf-preset-tag">${escapeHtml(p.regionTag)} · 1:${(p.scale / 1000).toFixed(0)}k</span>
          </button>
        `
      )
      .join("");
  }

  private hasAnyOutputEnabled(): boolean {
    const o = this.state.outputs;
    return (
      o.pdf ||
      o.mbtiles ||
      o.kmz ||
      o.gpkg_tiles ||
      o.osmand_sqlitedb ||
      o.field_bundle_zip
    );
  }

  private bindEvents(): void {
    const submitCenterOrPlace = async () => {
      const value = this.centerInput.value.trim();
      if (!value) return;

      if (
        !this.geocodeMenuEl.classList.contains("hidden") &&
        this.geocodeHighlightIdx >= 0 &&
        this.geocodeResults[this.geocodeHighlightIdx]
      ) {
        const picked = this.geocodeResults[this.geocodeHighlightIdx]!;
        this.choosePlace(picked);
        return;
      }

      if (looksLikeCoordOrMgrs(value)) {
        this.closeGeocodeMenu();
        this.activePresetId = null;
        this.updatePresetButtons();
        this.setFormError(null);
        this.callbacks.onCenterSubmit(value);
        return;
      }

      if (this.geocodeResults.length > 0 && this.geocodeResults[0]) {
        this.choosePlace(this.geocodeResults[0]);
        return;
      }

      this.cancelPendingGeocode();
      const controller = new AbortController();
      this.geocodeAbort = controller;
      this.showGeocodeStatus("Searching places…");
      try {
        const res = await fetchGeocode(value, 5, controller.signal);
        if (controller.signal.aborted) return;
        if (res.results.length > 0 && res.results[0]) {
          this.choosePlace(res.results[0]);
        } else {
          this.closeGeocodeMenu();
          this.callbacks.onPlaceNotFound(value);
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          if (
            err instanceof ApiError &&
            (err.status === 429 || err.status === 503)
          ) {
            this.showGeocodeStatus(err.message);
            this.callbacks.onApiError?.(err, "geocode");
          } else {
            this.closeGeocodeMenu();
            this.callbacks.onPlaceNotFound(value);
          }
        }
      }
    };

    this.q("[data-role='go-center']").addEventListener("click", () => {
      void submitCenterOrPlace();
    });

    this.centerInput.addEventListener("input", () => {
      this.setFormError(null);
      const value = this.centerInput.value.trim();
      if (value.length < 2 || looksLikeCoordOrMgrs(value)) {
        this.closeGeocodeMenu();
        return;
      }
      this.scheduleGeocode(value);
    });

    this.centerInput.addEventListener("keydown", (e) => {
      const menuOpen = !this.geocodeMenuEl.classList.contains("hidden");
      if (e.key === "ArrowDown" && menuOpen && this.geocodeResults.length > 0) {
        e.preventDefault();
        this.geocodeHighlightIdx =
          (this.geocodeHighlightIdx + 1) % this.geocodeResults.length;
        this.renderGeocodeResults();
        return;
      }
      if (e.key === "ArrowUp" && menuOpen && this.geocodeResults.length > 0) {
        e.preventDefault();
        this.geocodeHighlightIdx =
          (this.geocodeHighlightIdx - 1 + this.geocodeResults.length) %
          this.geocodeResults.length;
        this.renderGeocodeResults();
        return;
      }
      if (e.key === "Escape" && menuOpen) {
        e.preventDefault();
        e.stopPropagation();
        this.closeGeocodeMenu();
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        void submitCenterOrPlace();
      }
    });

    this.geocodeMenuEl.addEventListener("mousedown", (e) => {
      e.preventDefault();
    });

    this.geocodeMenuEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const item = target?.closest<HTMLButtonElement>("[data-geocode-idx]");
      if (!item) return;
      const idx = Number(item.dataset.geocodeIdx);
      const picked = this.geocodeResults[idx];
      if (picked) {
        this.choosePlace(picked);
      }
    });

    document.addEventListener("click", (e) => {
      const target = e.target as Node | null;
      if (target && !this.geocodeWrapEl.contains(target)) {
        this.closeGeocodeMenu();
      }
    });

    // Preset Region Tabs
    this.presetTabsEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("[data-preset-tab]");
      if (!btn) return;
      const tab = btn.dataset.presetTab as PresetRegionTab;
      if (!tab || tab === this.activePresetTab) return;
      this.activePresetTab = tab;
      this.presetTabsEl
        .querySelectorAll<HTMLButtonElement>("[data-preset-tab]")
        .forEach((b) => b.classList.toggle("active", b === btn));
      this.renderPresetCards();
    });

    // Delegated Preset Card clicks
    this.presetGridEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("[data-preset]");
      if (!btn) return;
      const preset = LOCATION_PRESETS.find((p) => p.id === btn.dataset.preset);
      if (!preset) return;
      this.closeGeocodeMenu();
      this.activePresetId = preset.id;
      this.setFormError(null);
      this.callbacks.onPresetSelect(preset);
    });

    this.titleInput.addEventListener("input", () => {
      this.state.title = this.titleInput.value;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.scaleSelect.addEventListener("change", () => {
      this.state.scale = Number(this.scaleSelect.value);
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onFootprintParamsChange();
    });

    this.paperSelect.addEventListener("change", () => {
      this.state.paper = this.paperSelect.value as PaperSize;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onFootprintParamsChange();
    });

    const syncAtlasIndexChecks = (checked: boolean) => {
      this.state.include_atlas_index = checked;
      this.sheetAtlasIndexCheck.checked = checked;
      this.outAtlasIndexCheck.checked = checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    };

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-atlas-cols]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          if (this.state.stripPlan) return;
          const cols = Number(btn.dataset.atlasCols);
          const rows = Number(btn.dataset.atlasRows);
          if (!isAtlasOptionAllowed(cols, rows, this.session.capabilities)) {
            return;
          }
          if (
            this.state.grid_cols === cols &&
            this.state.grid_rows === rows
          ) {
            return;
          }
          const wasSingle = this.state.grid_cols * this.state.grid_rows === 1;
          this.state.grid_cols = cols;
          this.state.grid_rows = rows;
          if (wasSingle && cols * rows > 1) {
            this.state.include_atlas_index = true;
            this.sheetAtlasIndexCheck.checked = true;
            this.outAtlasIndexCheck.checked = true;
          } else if (cols * rows === 1) {
            this.state.include_atlas_index = false;
            this.sheetAtlasIndexCheck.checked = false;
            this.outAtlasIndexCheck.checked = false;
          }
          this.setFormError(null);
          this.updateAtlasButtons();
          this.updateSectionPills();
          this.callbacks.onFootprintParamsChange({ fitAfterLoad: true });
        });
      });

    this.foldMarksCheck.addEventListener("change", () => {
      this.state.show_fold_marks = this.foldMarksCheck.checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.sheetAtlasIndexCheck.addEventListener("change", () => {
      syncAtlasIndexChecks(this.sheetAtlasIndexCheck.checked);
    });

    this.bindSegmented("orientation", (val) => {
      this.state.orientation = val as Orientation;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onFootprintParamsChange();
    });

    this.bindSegmented("color_mode", (val) => {
      this.state.color_mode = val as ColorMode;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.bindSegmented("units", (val) => {
      this.state.units = val as Units;
      this.setFormError(null);
      this.callbacks.onFootprintParamsChange();
    });

    this.demSelect.addEventListener("change", () => {
      this.state.dem_source = this.demSelect.value as DemSourceKind;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.packSelect.addEventListener("change", () => {
      this.state.pack_id = this.packSelect.value;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onPackChange(this.state.pack_id);
      this.callbacks.onStateChange();
    });

    this.q("[data-role='manage-packs']").addEventListener("click", () => {
      this.callbacks.onManagePacks();
    });

    this.q("[data-role='refresh-packs']").addEventListener("click", () => {
      this.callbacks.onRefreshPacks();
    });

    this.stateAutoCheck.addEventListener("change", () => {
      const others = this.state.au_overlays.filter(
        (k) => k !== "au-state-trails" && !STATE_TRAIL_CHIP_SET.has(k)
      );
      this.state.au_overlays = this.stateAutoCheck.checked
        ? ["au-state-trails", ...others]
        : others;
      this.setFormError(null);
      this.syncStateTrailControls();
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-au-state-chip]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const kind = btn.dataset.auStateChip as AUStateTrailKind;
          const withoutAuto = this.state.au_overlays.filter(
            (k) => k !== "au-state-trails"
          );
          if (withoutAuto.includes(kind)) {
            this.state.au_overlays = withoutAuto.filter((k) => k !== kind);
          } else {
            this.state.au_overlays = [...withoutAuto, kind];
          }
          this.setFormError(null);
          this.syncStateTrailControls();
          this.updateSectionPills();
          this.callbacks.onStateChange();
        });
      });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-au-overlay]")
      .forEach((cb) => {
        cb.addEventListener("change", () => {
          const kind = cb.dataset.auOverlay as AUOverlayKind;
          if (cb.checked) {
            if (!this.state.au_overlays.includes(kind)) {
              this.state.au_overlays = [...this.state.au_overlays, kind];
            }
          } else {
            this.state.au_overlays = this.state.au_overlays.filter(
              (k) => k !== kind
            );
          }
          this.setFormError(null);
          this.updateSectionPills();
          this.callbacks.onStateChange();
        });
      });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-nz-overlay]")
      .forEach((cb) => {
        cb.addEventListener("change", () => {
          const kind = cb.dataset.nzOverlay as NZOverlayKind;
          if (cb.checked) {
            if (!this.state.nz_overlays.includes(kind)) {
              this.state.nz_overlays = [...this.state.nz_overlays, kind];
            }
          } else {
            this.state.nz_overlays = this.state.nz_overlays.filter(
              (k) => k !== kind
            );
          }
          this.setFormError(null);
          this.updateSectionPills();
          this.callbacks.onStateChange();
        });
      });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-ca-overlay]")
      .forEach((cb) => {
        cb.addEventListener("change", () => {
          const kind = cb.dataset.caOverlay as CAOverlayKind;
          if (cb.checked) {
            if (!this.state.ca_overlays.includes(kind)) {
              this.state.ca_overlays = [...this.state.ca_overlays, kind];
            }
          } else {
            this.state.ca_overlays = this.state.ca_overlays.filter(
              (k) => k !== kind
            );
          }
          this.setFormError(null);
          this.updateSectionPills();
          this.callbacks.onStateChange();
        });
      });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-us-overlay]")
      .forEach((cb) => {
        cb.addEventListener("change", () => {
          const kind = cb.dataset.usOverlay as USOverlayKind;
          if (cb.checked) {
            if (!this.state.us_overlays.includes(kind)) {
              this.state.us_overlays = [...this.state.us_overlays, kind];
            }
          } else {
            this.state.us_overlays = this.state.us_overlays.filter(
              (k) => k !== kind
            );
          }
          this.setFormError(null);
          this.updateSectionPills();
          this.callbacks.onStateChange();
        });
      });

    this.contourSelect.addEventListener("change", () => {
      this.state.carto.contour_density = this.contourSelect
        .value as DensityLevel;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.hillshadeSelect.addEventListener("change", () => {
      this.state.carto.hillshade_strength = this.hillshadeSelect
        .value as HillshadeStrength;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.labelDensitySelect.addEventListener("change", () => {
      this.state.carto.label_density = this.labelDensitySelect
        .value as DensityLevel;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-carto-toggle]")
      .forEach((cb) => {
        cb.addEventListener("change", () => {
          const key = cb.dataset.cartoToggle as keyof typeof this.state.carto;
          if (
            key === "show_buildings" ||
            key === "show_trees" ||
            key === "show_power" ||
            key === "show_pipelines" ||
            key === "show_grid" ||
            key === "show_river_arrows"
          ) {
            this.state.carto[key] = cb.checked;
            this.setFormError(null);
            this.updateSectionPills();
            this.callbacks.onStateChange();
          }
        });
      });

    this.slopeCheck.addEventListener("change", () => {
      this.state.carto.show_slope_shading = this.slopeCheck.checked;
      this.setFormError(null);
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    const handleFile = (file: File | null) => {
      if (file && file.size > this.maxUploadBytes) {
        this.setFormError(
          `Overlay file exceeds ${formatUploadLimitLabel(this.maxUploadBytes)} limit.`
        );
        return;
      }
      this.setFormError(null);
      this.state.overlayFile = file;
      this.syncFileDisplay();
      this.updateSectionPills();
      this.callbacks.onOverlayFileChange(file);
    };

    this.fileInput.addEventListener("change", () => {
      handleFile(this.fileInput.files?.[0] ?? null);
    });

    this.fileClearBtn.addEventListener("click", () => {
      this.fileInput.value = "";
      handleFile(null);
    });

    this.dropzoneEl.addEventListener("dragover", (e) => {
      e.preventDefault();
      this.dropzoneEl.classList.add("dragover");
    });

    this.dropzoneEl.addEventListener("dragleave", () => {
      this.dropzoneEl.classList.remove("dragover");
    });

    this.dropzoneEl.addEventListener("drop", (e) => {
      e.preventDefault();
      this.dropzoneEl.classList.remove("dragover");
      const file = e.dataTransfer?.files?.[0] ?? null;
      if (file) handleFile(file);
    });

    this.stripBarEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-strip-action]");
      if (!btn) return;
      this.setFormError(null);
      const action = btn.dataset.stripAction;
      if (action === "plan") {
        if (!this.session.capabilities.strip_atlas_allowed) return;
        this.callbacks.onAutoStripPlan();
      } else if (action === "clear") {
        this.callbacks.onClearStripPlan();
      }
    });

    // Section 05 Waypoints & Tactical Markup
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-tool-btn]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const tool = btn.dataset.toolBtn as MapInteractionMode;
          this.callbacks.onToggleInteractionMode(tool);
        });
      });

    this.rulerBoxEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-ruler-action]");
      if (!btn) return;
      if (btn.dataset.rulerAction === "pin") {
        this.setFormError(null);
        this.callbacks.onRulerPinToPdf();
      } else if (btn.dataset.rulerAction === "clear") {
        this.callbacks.onRulerClear();
      }
    });

    this.sectorDraftBarEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>(
        "button[data-sector-action]"
      );
      if (!btn) return;
      if (btn.dataset.sectorAction === "finish") {
        this.setFormError(null);
        this.callbacks.onSectorFinish();
      } else if (btn.dataset.sectorAction === "cancel") {
        this.callbacks.onSectorCancel();
      }
    });

    this.ringRadiiInput.addEventListener("change", () => {
      const radii = parseRingRadiiText(this.ringRadiiInput.value);
      this.ringRadiiInput.value = radii.join(", ");
      this.callbacks.onRingRadiiChange(radii);
    });

    this.tacticalClearBtn.addEventListener("click", () => {
      this.setFormError(null);
      this.callbacks.onTacticalClear();
    });

    this.tacticalListEl.addEventListener("change", (e) => {
      const target = e.target as HTMLElement | null;
      if (target instanceof HTMLInputElement && target.dataset.tgLabel) {
        const idx = Number(target.dataset.tgLabel);
        this.setFormError(null);
        this.callbacks.onTacticalLabelEdit(
          idx,
          target.value.trim() || `TG${idx + 1}`
        );
      }
    });

    this.tacticalListEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-tg-del]");
      if (!btn) return;
      this.setFormError(null);
      this.callbacks.onTacticalDelete(Number(btn.dataset.tgDel));
    });

    this.wpClearBtn.addEventListener("click", () => {
      this.setFormError(null);
      this.callbacks.onWaypointsClear();
    });

    const syncRouteCardChecks = (checked: boolean) => {
      this.state.include_route_card = checked;
      this.wpRouteCardCheck.checked = checked;
      this.outRouteCardCheck.checked = checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    };

    this.wpRouteCardCheck.addEventListener("change", () => {
      syncRouteCardChecks(this.wpRouteCardCheck.checked);
    });

    this.wpListEl.addEventListener("change", (e) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      this.setFormError(null);
      if (target instanceof HTMLInputElement && target.dataset.wpName) {
        const idx = Number(target.dataset.wpName);
        this.callbacks.onWaypointEdit(idx, {
          label: target.value.trim() || `WP${idx + 1}`,
        });
      } else if (target instanceof HTMLSelectElement && target.dataset.wpCat) {
        const idx = Number(target.dataset.wpCat);
        this.callbacks.onWaypointEdit(idx, {
          category: target.value as WaypointCategory,
        });
      }
    });

    this.wpListEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-wp-del]");
      if (!btn) return;
      this.setFormError(null);
      this.callbacks.onWaypointDelete(Number(btn.dataset.wpDel));
    });

    // Section 06 RF / Viewshed + Point-to-Point RF Link
    this.vsToggleBtn.addEventListener("click", () => {
      this.callbacks.onToggleInteractionMode("los");
    });

    this.vsClearBtn.addEventListener("click", () => {
      this.setFormError(null);
      this.callbacks.onViewshedsClear();
    });

    const clampNum = (
      val: string,
      min: number,
      max: number,
      fallback: number
    ) => {
      const n = Number(val);
      if (!Number.isFinite(n)) return fallback;
      return Math.min(max, Math.max(min, n));
    };

    this.vsDefObsInput.addEventListener("change", () => {
      const v = clampNum(this.vsDefObsInput.value, 0, 500, 2);
      this.state.viewshedDefaults.observer_height_m = v;
      this.vsDefObsInput.value = String(v);
    });

    this.vsDefTgtInput.addEventListener("change", () => {
      const v = clampNum(this.vsDefTgtInput.value, 0, 500, 1.5);
      this.state.viewshedDefaults.target_height_m = v;
      this.vsDefTgtInput.value = String(v);
    });

    this.vsDefRadInput.addEventListener("change", () => {
      const v = clampNum(this.vsDefRadInput.value, 0.25, 25, 10);
      this.state.viewshedDefaults.max_distance_km = v;
      this.vsDefRadInput.value = String(v);
    });

    this.vsListEl.addEventListener("change", (e) => {
      const target = e.target as HTMLElement | null;
      if (!(target instanceof HTMLInputElement)) return;
      const id = target.dataset.vsId;
      const field = target.dataset.vsField;
      if (!id || !field) return;
      this.setFormError(null);
      if (field === "label") {
        this.callbacks.onViewshedEdit(
          id,
          { label: target.value.trim() || "Observer" },
          false
        );
      } else if (field === "obs_h") {
        this.callbacks.onViewshedEdit(
          id,
          { observer_height_m: clampNum(target.value, 0, 500, 2) },
          true
        );
      } else if (field === "tgt_h") {
        this.callbacks.onViewshedEdit(
          id,
          { target_height_m: clampNum(target.value, 0, 500, 1.5) },
          true
        );
      } else if (field === "rad_km") {
        this.callbacks.onViewshedEdit(
          id,
          { max_distance_m: clampNum(target.value, 0.25, 25, 10) * 1000 },
          true
        );
      }
    });

    this.vsListEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-vs-action]");
      if (!btn) return;
      const action = btn.dataset.vsAction;
      const id = btn.dataset.vsId;
      if (!id) return;
      this.setFormError(null);
      if (action === "delete") {
        this.callbacks.onViewshedDelete(id);
      } else if (action === "recompute") {
        this.callbacks.onViewshedRecompute(id);
      }
    });

    // RF Point-to-Point Link controls
    this.rfPickBtn.addEventListener("click", () => {
      this.callbacks.onToggleInteractionMode("rf-link");
    });

    this.rfUseObsBtn.addEventListener("click", () => {
      this.setFormError(null);
      this.callbacks.onRFLinkUseObservers();
    });

    this.rfClearBtn.addEventListener("click", () => {
      this.setFormError(null);
      this.callbacks.onRFLinksClear();
    });

    this.rfBandSelect.addEventListener("change", () => {
      if (this.rfBandSelect.value !== "custom") {
        const freq = Number(this.rfBandSelect.value);
        if (Number.isFinite(freq)) {
          this.state.rfLinkDefaults.freq_mhz = freq;
          this.rfFreqInput.value = String(freq);
        }
      }
    });

    this.rfFreqInput.addEventListener("change", () => {
      const v = clampNum(this.rfFreqInput.value, 30, 60000, 915);
      this.state.rfLinkDefaults.freq_mhz = v;
      this.rfFreqInput.value = String(v);
      const match = RF_BAND_PRESETS.find((b) => Math.abs(b.freq_mhz - v) < 0.05);
      this.rfBandSelect.value = match ? String(match.freq_mhz) : "custom";
    });

    this.rfTxHInput.addEventListener("change", () => {
      const v = clampNum(this.rfTxHInput.value, 0, 500, 2);
      this.state.rfLinkDefaults.from_height_m = v;
      this.rfTxHInput.value = String(v);
    });

    this.rfRxHInput.addEventListener("change", () => {
      const v = clampNum(this.rfRxHInput.value, 0, 500, 2);
      this.state.rfLinkDefaults.to_height_m = v;
      this.rfRxHInput.value = String(v);
    });

    const syncRfCardChecks = (checked: boolean) => {
      this.state.include_rf_link_card = checked;
      this.rfCardCheck.checked = checked;
      this.outRfLinkCardCheck.checked = checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    };

    this.rfCardCheck.addEventListener("change", () => {
      syncRfCardChecks(this.rfCardCheck.checked);
    });

    this.rfLinksListEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-rf-action]");
      if (!btn) return;
      const id = btn.dataset.rfId;
      if (!id) return;
      this.setFormError(null);
      if (btn.dataset.rfAction === "delete") {
        this.callbacks.onRFLinkDelete(id);
      } else if (btn.dataset.rfAction === "recompute") {
        this.callbacks.onRFLinkRecompute(id);
      }
    });

    // Outputs
    const syncOutputControls = () => {
      this.syncOutputLockStates();

      if (!this.hasAnyOutputEnabled()) {
        this.setFormError(
          "Select at least one output (PDF, MBTiles, KMZ, GPKG, SQLiteDB, or Bundle)."
        );
      } else {
        this.setFormError(null);
      }
    };

    this.outPdfCheck.addEventListener("change", () => {
      this.state.outputs.pdf = this.outPdfCheck.checked;
      syncOutputControls();
      this.callbacks.onFootprintParamsChange();
    });

    this.outGeoPdfCheck.addEventListener("change", () => {
      this.state.outputs.geopdf = this.outGeoPdfCheck.checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.outCalibrationCheck.addEventListener("change", () => {
      this.state.include_calibration_page = this.outCalibrationCheck.checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.outRouteCardCheck.addEventListener("change", () => {
      syncRouteCardChecks(this.outRouteCardCheck.checked);
    });

    this.outElevProfileCheck.addEventListener("change", () => {
      this.state.include_elevation_profile = this.outElevProfileCheck.checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.outAtlasIndexCheck.addEventListener("change", () => {
      syncAtlasIndexChecks(this.outAtlasIndexCheck.checked);
    });

    this.outRfLinkCardCheck.addEventListener("change", () => {
      syncRfCardChecks(this.outRfLinkCardCheck.checked);
    });

    this.outMbtilesCheck.addEventListener("change", () => {
      this.state.outputs.mbtiles = this.outMbtilesCheck.checked;
      syncOutputControls();
      this.callbacks.onStateChange();
    });

    this.outMbtilesFmt.addEventListener("change", () => {
      this.state.outputs.mbtiles_format = this.outMbtilesFmt
        .value as MBTilesFormat;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.outKmzCheck.addEventListener("change", () => {
      this.state.outputs.kmz = this.outKmzCheck.checked;
      syncOutputControls();
      this.callbacks.onStateChange();
    });

    this.outGarminProfile.addEventListener("change", () => {
      this.state.outputs.garmin_profile = this.outGarminProfile
        .value as GarminProfile;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.outGpkgCheck.addEventListener("change", () => {
      this.state.outputs.gpkg_tiles = this.outGpkgCheck.checked;
      syncOutputControls();
      this.callbacks.onStateChange();
    });

    this.outOsmandCheck.addEventListener("change", () => {
      this.state.outputs.osmand_sqlitedb = this.outOsmandCheck.checked;
      syncOutputControls();
      this.callbacks.onStateChange();
    });

    this.outBundleCheck.addEventListener("change", () => {
      this.state.outputs.field_bundle_zip = this.outBundleCheck.checked;
      syncOutputControls();
      this.callbacks.onStateChange();
    });

    this.outDigitalGridCheck.addEventListener("change", () => {
      this.state.outputs.include_grid_on_digital =
        this.outDigitalGridCheck.checked;
      this.setFormError(null);
      this.callbacks.onStateChange();
    });

    this.outPdfDpiSelect.addEventListener("change", () => {
      const nextDpi = Number(this.outPdfDpiSelect.value);
      const maxDpi = getEffectiveMaxDpi(this.session.capabilities);
      if (Number.isFinite(nextDpi) && nextDpi <= maxDpi) {
        this.state.dpi = nextDpi;
        this.updateSectionPills();
        this.setFormError(null);
        this.callbacks.onStateChange();
      } else {
        this.outPdfDpiSelect.value = String(this.state.dpi);
      }
    });

    this.outExportDpiSelect.addEventListener("change", () => {
      const nextDpi = Number(this.outExportDpiSelect.value);
      const maxDpi = getEffectiveMaxExportDpi(this.session.capabilities);
      if (Number.isFinite(nextDpi) && nextDpi <= maxDpi) {
        this.state.outputs.export_dpi = nextDpi;
        this.setFormError(null);
        this.callbacks.onStateChange();
      } else {
        this.outExportDpiSelect.value = String(this.state.outputs.export_dpi);
      }
    });

    this.quotaNoticeEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("[data-action='open-auth']");
      if (btn) {
        this.callbacks.onOpenAuth?.();
      }
    });

    this.generateBtn.addEventListener("click", () => {
      if (!this.hasAnyOutputEnabled()) {
        this.setFormError(
          "Select at least one output (PDF, MBTiles, KMZ, GPKG, SQLiteDB, or Bundle)."
        );
        return;
      }
      this.setFormError(null);
      this.callbacks.onSubmitJob();
    });

    this.q("[data-role='copy-cli']").addEventListener("click", () => {
      this.callbacks.onCopyCli();
    });
  }

  private cancelPendingGeocode(): void {
    if (this.geocodeTimer !== null) {
      window.clearTimeout(this.geocodeTimer);
      this.geocodeTimer = null;
    }
    this.geocodeAbort?.abort();
    this.geocodeAbort = null;
  }

  private scheduleGeocode(query: string): void {
    this.cancelPendingGeocode();
    this.geocodeTimer = window.setTimeout(() => {
      this.geocodeTimer = null;
      void this.runGeocode(query);
    }, GEOCODE_DEBOUNCE_MS);
  }

  private async runGeocode(query: string): Promise<void> {
    const controller = new AbortController();
    this.geocodeAbort = controller;
    this.showGeocodeStatus("Searching places…");

    try {
      const res = await fetchGeocode(query, 5, controller.signal);
      if (controller.signal.aborted) return;
      this.geocodeResults = res.results;
      this.geocodeHighlightIdx = res.results.length > 0 ? 0 : -1;
      if (res.results.length === 0) {
        this.showGeocodeStatus("No matching places found");
      } else {
        this.renderGeocodeResults();
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        if (
          err instanceof ApiError &&
          (err.status === 429 || err.status === 503)
        ) {
          this.showGeocodeStatus(err.message);
          this.callbacks.onApiError?.(err, "geocode");
        } else {
          this.closeGeocodeMenu();
        }
      }
    }
  }

  private showGeocodeStatus(text: string): void {
    this.geocodeMenuEl.innerHTML = `<div class="gf-geocode-status">${escapeHtml(text)}</div>`;
    this.geocodeMenuEl.classList.remove("hidden");
  }

  private renderGeocodeResults(): void {
    this.geocodeMenuEl.innerHTML = this.geocodeResults
      .map((r, idx) => {
        const activeClass =
          idx === this.geocodeHighlightIdx ? " highlighted" : "";
        return `
          <button
            type="button"
            class="gf-geocode-item${activeClass}"
            data-geocode-idx="${idx}"
            role="option"
            aria-selected="${idx === this.geocodeHighlightIdx}"
          >
            <span class="gf-geocode-head">
              <span class="gf-geocode-name">${escapeHtml(r.short_name)}</span>
              <span class="gf-geocode-type">${escapeHtml(r.type)}</span>
            </span>
            <span class="gf-geocode-sub">${escapeHtml(r.display_name)}</span>
            <span class="gf-geocode-coords">${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}</span>
          </button>
        `;
      })
      .join("");
    this.geocodeMenuEl.classList.remove("hidden");
  }

  private choosePlace(place: GeocodeResult): void {
    this.closeGeocodeMenu();
    this.activePresetId = null;
    this.updatePresetButtons();
    this.setFormError(null);
    this.callbacks.onPlaceSelect(place);
  }

  closeGeocodeMenu(): void {
    this.cancelPendingGeocode();
    this.geocodeResults = [];
    this.geocodeHighlightIdx = -1;
    this.geocodeMenuEl.classList.add("hidden");
    this.geocodeMenuEl.innerHTML = "";
  }

  private bindSegmented(name: string, onChange: (val: string) => void): void {
    const group = this.q(`[data-seg='${name}']`);
    group
      .querySelectorAll<HTMLButtonElement>("button[data-value]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const val = btn.dataset.value;
          if (!val) return;
          group
            .querySelectorAll<HTMLButtonElement>("button[data-value]")
            .forEach((b) => b.classList.toggle("active", b === btn));
          onChange(val);
        });
      });
  }

  private setSegmentedValue(name: string, value: string): void {
    const group = this.q(`[data-seg='${name}']`);
    group
      .querySelectorAll<HTMLButtonElement>("button[data-value]")
      .forEach((b) => b.classList.toggle("active", b.dataset.value === value));
  }

  private updateAtlasButtons(): void {
    const stripActive = this.state.stripPlan !== null;
    const cap = this.session.capabilities;
    this.atlasGridEl.classList.toggle("disabled", stripActive);
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-atlas-cols]")
      .forEach((btn) => {
        const cols = Number(btn.dataset.atlasCols);
        const rows = Number(btn.dataset.atlasRows);
        const tierAllowed = isAtlasOptionAllowed(cols, rows, cap);
        btn.disabled = stripActive || !tierAllowed;
        btn.classList.toggle("tier-locked", !tierAllowed);
        if (!tierAllowed) {
          btn.title = "Friend invite required for multi-sheet atlases";
        } else {
          btn.title =
            cols * rows === 1
              ? "Single sheet"
              : `${cols * rows} edge-matched sheets`;
        }
        const active =
          !stripActive &&
          cols === this.state.grid_cols &&
          rows === this.state.grid_rows;
        btn.classList.toggle("active", active);
      });

    if (this.session.auth_mode === "hosted" && cap.max_grid_cells <= 1) {
      this.atlasTierHintEl.textContent =
        "Multi-sheet atlases require an invited Friend account.";
      this.atlasTierHintEl.classList.remove("hidden");
    } else {
      this.atlasTierHintEl.textContent = "";
      this.atlasTierHintEl.classList.add("hidden");
    }

    if (stripActive && this.state.stripPlan) {
      const count =
        this.state.stripPlan.sheet_count ?? this.state.stripPlan.sheets.length;
      this.atlasSublabelEl.textContent = `Strip Atlas active (${count} sheets)`;
    } else {
      const count = this.state.grid_cols * this.state.grid_rows;
      this.atlasSublabelEl.textContent =
        count === 1
          ? "Single sheet"
          : `${count} edge-matched sheets (multi-page PDF)`;
    }
  }

  private syncStateTrailControls(): void {
    const autoOn = this.state.au_overlays.includes("au-state-trails");
    this.stateAutoCheck.checked = autoOn;
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-au-state-chip]")
      .forEach((btn) => {
        const kind = btn.dataset.auStateChip as AUStateTrailKind;
        const pinned = !autoOn && this.state.au_overlays.includes(kind);
        btn.classList.toggle("active", pinned);
        btn.setAttribute("aria-pressed", String(pinned));
      });
  }

  private updatePresetButtons(): void {
    this.presetGridEl
      .querySelectorAll<HTMLButtonElement>("[data-preset]")
      .forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.preset === this.activePresetId);
      });
  }

  private syncFileDisplay(): void {
    const file = this.state.overlayFile;
    if (file) {
      this.fileTitleEl.textContent = file.name;
      this.fileSubEl.textContent = `${formatBytes(file.size)} · Ready to attach`;
      this.fileClearBtn.hidden = false;
    } else {
      this.fileTitleEl.textContent = "Drop or browse overlay file…";
      this.fileSubEl.textContent = `Waypoints, tracks, or polygons (max ${formatUploadLimitLabel(this.maxUploadBytes)})`;
      this.fileClearBtn.hidden = true;
    }
    this.renderStripBar();
  }

  private renderStripBar(): void {
    const eligible = isStripEligibleFile(this.state.overlayFile);
    const plan = this.state.stripPlan;

    if (!eligible && !plan) {
      this.stripBarEl.classList.add("hidden");
      this.stripBarEl.innerHTML = "";
      return;
    }

    this.stripBarEl.classList.remove("hidden");

    if (plan) {
      const count = plan.sheet_count ?? plan.sheets.length;
      this.stripBarEl.innerHTML = `
        <div class="gf-strip-summary">
          <span class="gf-strip-badge mono">Strip Atlas: ${count} sheet${count === 1 ? "" : "s"} · ${plan.route_distance_km.toFixed(1)} km</span>
          <button type="button" class="gf-mini-btn danger" data-strip-action="clear">Clear Strip</button>
        </div>
      `;
    } else if (!this.session.capabilities.strip_atlas_allowed) {
      this.stripBarEl.innerHTML = `
        <button
          type="button"
          class="gf-strip-btn tier-locked"
          data-strip-action="plan"
          disabled
          title="Friend invite required for Auto-Fit Strip Atlas"
        >
          <span>Auto-Fit Strip Atlas (Friend invite required)</span>
        </button>
      `;
    } else {
      this.stripBarEl.innerHTML = `
        <button
          type="button"
          class="gf-strip-btn"
          data-strip-action="plan"
          ${this.planningStrip ? "disabled" : ""}
        >
          <span>${this.planningStrip ? "Planning Strip Atlas…" : "Auto-Fit Strip Atlas"}</span>
        </button>
      `;
    }
  }

  private syncToolButtons(): void {
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-tool-btn]")
      .forEach((btn) => {
        const active = btn.dataset.toolBtn === this.interactionMode;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-pressed", String(active));
      });
    this.vsToggleBtn.classList.toggle("active", this.interactionMode === "los");
    this.vsToggleBtn.innerHTML =
      this.interactionMode === "los"
        ? `<span>📡 LOS ON (Click Map)</span>`
        : `<span>📡 Drop Observer</span>`;

    this.rfPickBtn.classList.toggle("active", this.interactionMode === "rf-link");
    this.rfPickBtn.innerHTML =
      this.interactionMode === "rf-link"
        ? this.rfPendingFrom
          ? `<span>⚡ Click Point B…</span>`
          : `<span>⚡ Click Point A…</span>`
        : `<span>⚡ A ↔ B Pick</span>`;
  }

  private renderRulerBox(): void {
    const count = this.rulerPoints.length;
    if (count === 0 && this.interactionMode !== "ruler") {
      this.rulerBoxEl.classList.add("hidden");
      this.rulerBoxEl.innerHTML = "";
      return;
    }

    this.rulerBoxEl.classList.remove("hidden");
    if (count === 0) {
      this.rulerBoxEl.innerHTML = `
        <div class="gf-ruler-head">
          <span class="gf-ruler-title">📏 Ruler Active</span>
          <span class="gf-sublabel">Click map points to measure</span>
        </div>
      `;
      return;
    }

    const totalM = this.rulerMetrics?.total_distance_m ?? 0;
    const useImp = this.state.units === "imperial";
    const totalStr =
      count < 2
        ? "Click next point…"
        : this.rulerLoading
          ? "Computing…"
          : useImp
            ? `${(totalM / 1609.344).toFixed(2)} mi (${(totalM / 1000).toFixed(2)} km)`
            : `${(totalM / 1000).toFixed(2)} km (${Math.round(totalM)} m)`;

    const lastSeg =
      this.rulerMetrics && this.rulerMetrics.segments.length > 0
        ? this.rulerMetrics.segments[this.rulerMetrics.segments.length - 1]
        : null;

    const segMeta = lastSeg
      ? `
        <div class="gf-ruler-meta mono">
          <span>Mag ${Math.round(lastSeg.bearing_mag_deg).toString().padStart(3, "0")}° (${Math.round(lastSeg.bearing_mils)}m)</span>
          <span>True ${Math.round(lastSeg.bearing_true_deg).toString().padStart(3, "0")}° · Grid ${Math.round(lastSeg.bearing_grid_deg).toString().padStart(3, "0")}°</span>
          ${typeof lastSeg.grade_pct === "number" ? `<span>Grade ${lastSeg.grade_pct >= 0 ? "+" : ""}${lastSeg.grade_pct.toFixed(1)}%</span>` : ""}
        </div>
      `
      : "";

    this.rulerBoxEl.innerHTML = `
      <div class="gf-ruler-head">
        <span class="gf-ruler-title mono">${escapeHtml(totalStr)}</span>
        <div class="gf-ruler-actions">
          <button type="button" class="gf-mini-btn gf-pin-btn" data-ruler-action="pin" ${count < 2 ? "disabled" : ""}>Pin to PDF</button>
          <button type="button" class="gf-mini-btn danger" data-ruler-action="clear">Clear</button>
        </div>
      </div>
      ${segMeta}
    `;
  }

  private renderSectorDraftBar(): void {
    if (this.sectorDraftCount === 0 && this.interactionMode !== "sector") {
      this.sectorDraftBarEl.classList.add("hidden");
      this.sectorDraftBarEl.innerHTML = "";
      return;
    }
    this.sectorDraftBarEl.classList.remove("hidden");
    this.sectorDraftBarEl.innerHTML = `
      <div class="gf-ruler-head">
        <span class="gf-ruler-title mono">⬠ Sector Draft: ${this.sectorDraftCount} pt${this.sectorDraftCount === 1 ? "" : "s"}</span>
        <div class="gf-ruler-actions">
          <button type="button" class="gf-mini-btn gf-pin-btn" data-sector-action="finish" ${this.sectorDraftCount < 3 ? "disabled" : ""}>Finish</button>
          <button type="button" class="gf-mini-btn danger" data-sector-action="cancel">Cancel</button>
        </div>
      </div>
    `;
  }

  renderTacticalGraphicsList(): void {
    const list = this.state.tacticalGraphics;
    this.tacticalClearBtn.disabled = list.length === 0;
    this.updateWaypointsPill();

    if (list.length === 0) {
      this.tacticalListEl.innerHTML = "";
      return;
    }

    this.tacticalListEl.innerHTML = list
      .map((g, idx) => {
        const badge =
          g.kind === "range_ring"
            ? "RING"
            : g.kind === "sector"
              ? "AOI"
              : "LINE";
        const rawColor =
          typeof g.color === "string" && /^#[0-9a-fA-F]{6}$/.test(g.color.trim())
            ? g.color.trim()
            : null;
        const color =
          rawColor ||
          (g.kind === "range_ring"
            ? "#38BDF8"
            : g.kind === "sector"
              ? "#F59E0B"
              : "#F97316");
        const meta =
          g.kind === "range_ring"
            ? `Radii: ${g.radii_km.join(", ")} km · ${g.points[0] ? `${g.points[0][0].toFixed(4)}, ${g.points[0][1].toFixed(4)}` : ""}`
            : g.kind === "sector"
              ? `${g.points.length} vertices polygon`
              : `${g.points.length} pts bearing line`;

        return `
          <div class="gf-wp-row">
            <div class="gf-wp-top">
              <span class="gf-wp-code mono" style="border-color:${color};color:${color}">${badge}</span>
              <input
                type="text"
                class="gf-wp-name-input"
                data-tg-label="${idx}"
                value="${escapeHtml(g.label)}"
                maxlength="48"
                aria-label="Tactical graphic ${idx + 1} label"
              />
              <button type="button" class="gf-wp-del" data-tg-del="${idx}" title="Remove graphic">×</button>
            </div>
            <div class="gf-wp-meta mono">
              <span>${escapeHtml(meta)}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  private updateWaypointsPill(): void {
    const wps = this.state.waypoints;
    const tgs = this.state.tacticalGraphics.length;
    const parts: string[] = [];
    if (wps.length > 0) {
      const useImperial = this.state.units === "imperial";
      if (wps.length >= 2 && this.routeMetrics) {
        parts.push(
          `${wps.length} WPs · ${useImperial ? `${this.routeMetrics.total_distance_mi.toFixed(1)} mi` : `${this.routeMetrics.total_distance_km.toFixed(1)} km`}`
        );
      } else {
        parts.push(`${wps.length} WP${wps.length === 1 ? "" : "s"}`);
      }
    }
    if (tgs > 0) {
      parts.push(`${tgs} TG`);
    }
    this.waypointsPillEl.textContent = parts.length > 0 ? parts.join(" · ") : "0 WPs";
  }

  private renderWaypointsList(): void {
    const wps = this.state.waypoints;
    this.wpClearBtn.disabled = wps.length === 0;
    this.syncToolButtons();
    this.updateWaypointsPill();

    if (wps.length === 0) {
      this.wpListEl.innerHTML = `
        <div class="gf-wp-empty">
          Use <strong>⌖ WP</strong> (<kbd>M</kbd>), <strong>📏 Ruler</strong> (<kbd>R</kbd>), <strong>◎ Ring</strong>, or <strong>⬠ Sector</strong> to annotate your sheet.
        </div>
      `;
      this.wpTotalEl.classList.add("hidden");
      this.wpTotalEl.textContent = "";
      return;
    }

    const totalKm = this.routeMetrics?.total_distance_km ?? 0;
    const totalMi = this.routeMetrics?.total_distance_mi ?? 0;
    const useImperial = this.state.units === "imperial";

    this.wpListEl.innerHTML = wps
      .map((wp, idx) => {
        const code = `WP${idx + 1}`;
        const mWp = this.routeMetrics?.waypoints[idx];
        const leg = idx > 0 ? this.routeMetrics?.legs[idx - 1] : undefined;
        const mgrsStr =
          mWp?.mgrs_10m ?? `${wp.lat.toFixed(4)}, ${wp.lon.toFixed(4)}`;
        const legStr =
          idx === 0
            ? "Start"
            : leg
              ? useImperial
                ? `${leg.leg_dist_mi.toFixed(2)} mi`
                : `${leg.leg_dist_km.toFixed(2)} km`
              : "…";
        const azStr =
          idx === 0
            ? "—"
            : leg
              ? `${Math.round(leg.mag_az_deg).toString().padStart(3, "0")}°M`
              : "…";
        const color = CATEGORY_COLOR_MAP[wp.category] ?? "#EC4899";

        const catOptions = WAYPOINT_CATEGORY_OPTIONS.map(
          (o) =>
            `<option value="${o.value}" ${o.value === wp.category ? "selected" : ""}>${escapeHtml(o.label)}</option>`
        ).join("");

        return `
          <div class="gf-wp-row">
            <div class="gf-wp-top">
              <span class="gf-wp-code mono" style="border-color:${color};color:${color}">${code}</span>
              <input
                type="text"
                class="gf-wp-name-input"
                data-wp-name="${idx}"
                value="${escapeHtml(wp.label)}"
                maxlength="60"
                aria-label="Waypoint ${idx + 1} name"
              />
              <select class="gf-wp-cat-select" data-wp-cat="${idx}" aria-label="Waypoint ${idx + 1} category">
                ${catOptions}
              </select>
              <button type="button" class="gf-wp-del" data-wp-del="${idx}" title="Remove ${code}">×</button>
            </div>
            <div class="gf-wp-meta mono">
              <span class="gf-wp-mgrs" title="10m MGRS">${escapeHtml(mgrsStr)}</span>
              <span>${escapeHtml(legStr)}</span>
              <span class="gf-wp-az">${escapeHtml(azStr)}</span>
            </div>
          </div>
        `;
      })
      .join("");

    if (wps.length >= 2 && this.routeMetrics) {
      const distText = useImperial
        ? `${totalMi.toFixed(2)} mi (${totalKm.toFixed(2)} km)`
        : `${totalKm.toFixed(2)} km`;
      this.wpTotalEl.textContent = `Total: ${distText} · ${wps.length - 1} leg${wps.length === 2 ? "" : "s"} · WMM2025 Mag Az`;
      this.wpTotalEl.classList.remove("hidden");
    } else {
      this.wpTotalEl.classList.add("hidden");
      this.wpTotalEl.textContent = "";
    }
  }

  private updateViewshedAndRFPill(): void {
    const obsCount = this.state.viewsheds.length;
    const rfCount = this.state.rfLinks.length;
    if (obsCount === 0 && rfCount === 0) {
      this.viewshedPillEl.textContent = "Off";
      return;
    }
    const parts: string[] = [];
    if (obsCount > 0) {
      const anyLoading = this.state.viewsheds.some((e) => e.loading);
      parts.push(anyLoading ? `${obsCount} OBS…` : `${obsCount} OBS`);
    }
    if (rfCount > 0) {
      const anyRfLoading = this.state.rfLinks.some((l) => l.loading);
      parts.push(anyRfLoading ? `${rfCount} RF…` : `${rfCount} RF`);
    }
    this.viewshedPillEl.textContent = parts.join(" · ");
  }

  renderViewshedsList(): void {
    const entries = this.state.viewsheds;
    this.vsClearBtn.disabled = entries.length === 0;
    this.rfUseObsBtn.disabled = entries.length < 2;
    this.syncToolButtons();
    this.updateViewshedAndRFPill();

    if (entries.length === 0) {
      this.vsListEl.innerHTML = `
        <div class="gf-wp-empty">
          Click <strong>📡 Drop Observer</strong> (<kbd>V</kbd>) and click a summit or repeater site to preview DEM line-of-sight RF coverage.
        </div>
      `;
      return;
    }

    this.vsListEl.innerHTML = entries
      .map((entry, idx) => {
        const code = `OBS${idx + 1}`;
        const o = entry.observer;
        const maxKm = Number((o.max_distance_m / 1000).toFixed(2));

        let statusHtml: string;
        if (entry.loading) {
          statusHtml = `<span class="gf-vs-loading">Computing DEM line-of-sight…</span>`;
        } else if (entry.error) {
          statusHtml = `<span class="gf-vs-err">${escapeHtml(entry.error)}</span>`;
        } else if (entry.preview) {
          const elevText =
            entry.preview.observer_elev_m !== null
              ? `${Math.round(entry.preview.observer_elev_m)} m`
              : "—";
          statusHtml = `
            <span class="gf-vs-area">Visible: ${entry.preview.visible_area_km2.toFixed(1)} km²</span>
            <span>Elev: ${escapeHtml(elevText)}</span>
            <span class="gf-wp-mgrs" title="Observer 10m MGRS">${escapeHtml(entry.preview.observer_mgrs_10m)}</span>
          `;
        } else {
          statusHtml = `<span>${o.lat.toFixed(4)}, ${o.lon.toFixed(4)}</span> <button type="button" class="gf-mini-btn" data-vs-action="recompute" data-vs-id="${escapeHtml(entry.id)}">Compute</button>`;
        }

        return `
          <div class="gf-wp-row gf-vs-row">
            <div class="gf-wp-top">
              <span class="gf-wp-code gf-vs-code mono">${code}</span>
              <input
                type="text"
                class="gf-wp-name-input"
                data-vs-id="${escapeHtml(entry.id)}"
                data-vs-field="label"
                value="${escapeHtml(o.label)}"
                maxlength="60"
                aria-label="Observer ${idx + 1} label"
              />
              <button type="button" class="gf-wp-del" data-vs-action="recompute" data-vs-id="${escapeHtml(entry.id)}" title="Recompute viewshed">↻</button>
              <button type="button" class="gf-wp-del" data-vs-action="delete" data-vs-id="${escapeHtml(entry.id)}" title="Remove ${code}">×</button>
            </div>
            <div class="gf-vs-params mono">
              <label>Obs <input type="number" min="0" max="500" step="0.5" value="${o.observer_height_m}" data-vs-id="${escapeHtml(entry.id)}" data-vs-field="obs_h" />m</label>
              <label>Tgt <input type="number" min="0" max="500" step="0.5" value="${o.target_height_m}" data-vs-id="${escapeHtml(entry.id)}" data-vs-field="tgt_h" />m</label>
              <label>R <input type="number" min="0.25" max="25" step="0.5" value="${maxKm}" data-vs-id="${escapeHtml(entry.id)}" data-vs-field="rad_km" />km</label>
            </div>
            <div class="gf-wp-meta gf-vs-meta mono">
              ${statusHtml}
            </div>
          </div>
        `;
      })
      .join("");
  }

  renderRFLinksList(): void {
    const links = this.state.rfLinks;
    this.rfClearBtn.disabled = links.length === 0;
    this.syncToolButtons();
    this.updateViewshedAndRFPill();

    if (links.length === 0) {
      this.rfLinksListEl.innerHTML = `
        <div class="gf-wp-empty">
          Click <strong>⚡ A ↔ B Pick</strong> and select two points on the map (or use <strong>OBS1 → OBS2</strong>) for live Fresnel zone &amp; FSPL clearance analysis.
        </div>
      `;
      return;
    }

    this.rfLinksListEl.innerHTML = links
      .map((entry) => {
        const s = entry.spec;
        let bodyHtml = "";
        let badgeHtml = `<span class="gf-rf-badge pending">PENDING</span>`;

        if (entry.loading) {
          badgeHtml = `<span class="gf-rf-badge pending">COMPUTING…</span>`;
          bodyHtml = `<div class="gf-wp-meta mono"><span class="gf-vs-loading">Sampling DEM terrain &amp; Fresnel zone…</span></div>`;
        } else if (entry.error) {
          badgeHtml = `<span class="gf-rf-badge obstructed">ERROR</span>`;
          bodyHtml = `<div class="gf-wp-meta mono"><span class="gf-vs-err">${escapeHtml(entry.error)}</span></div>`;
        } else if (entry.profile) {
          const p = entry.profile;
          const stClass = p.status.toLowerCase();
          badgeHtml = `<span class="gf-rf-badge ${escapeHtml(stClass)}">${escapeHtml(p.status)}</span>`;
          const chartSvg = renderRFLinkSvg(p);
          bodyHtml = `
            <div class="gf-rf-chart-wrap">${chartSvg}</div>
            <div class="gf-rf-metrics mono">
              <span>${p.distance_km.toFixed(2)} km</span>
              <span>FSPL ${p.fspl_db.toFixed(1)} dB</span>
              <span>F1 ${Math.round(p.min_fresnel_pct)}%</span>
              <span>Mag ${Math.round(p.bearing_mag_deg).toString().padStart(3, "0")}° (${Math.round(p.bearing_mils)}m)</span>
            </div>
          `;
        } else {
          bodyHtml = `<div class="gf-wp-meta mono"><span>Ready</span> <button type="button" class="gf-mini-btn" data-rf-action="recompute" data-rf-id="${escapeHtml(entry.id)}">Compute</button></div>`;
        }

        return `
          <div class="gf-wp-row gf-rf-card">
            <div class="gf-wp-top">
              <span class="gf-rf-title mono">${escapeHtml(s.from_label)} ↔ ${escapeHtml(s.to_label)} · ${s.freq_mhz} MHz</span>
              ${badgeHtml}
              <button type="button" class="gf-wp-del" data-rf-action="recompute" data-rf-id="${escapeHtml(entry.id)}" title="Recompute RF link">↻</button>
              <button type="button" class="gf-wp-del" data-rf-action="delete" data-rf-id="${escapeHtml(entry.id)}" title="Remove RF link">×</button>
            </div>
            ${bodyHtml}
          </div>
        `;
      })
      .join("");
  }

  private updateSectionPills(): void {
    const scaleVal =
      this.state.scale % 1000 === 0
        ? `${this.state.scale / 1000}k`
        : `${Number((this.state.scale / 1000).toFixed(1))}k`;
    const scaleK = `1:${scaleVal}`;
    const paperLbl = this.state.paper.toUpperCase();
    const orientShort = this.state.orientation === "landscape" ? "L" : "P";
    const dpiSuffix = this.state.dpi > 300 ? ` · ${this.state.dpi}` : "";
    const stripSuffix = this.state.stripPlan
      ? ` · Strip ${this.state.stripPlan.sheet_count ?? this.state.stripPlan.sheets.length}sh`
      : this.state.grid_cols * this.state.grid_rows > 1
        ? ` · ${this.state.grid_cols}×${this.state.grid_rows}`
        : "";
    this.sheetPillEl.textContent = `${scaleK} · ${paperLbl} ${orientShort}${dpiSuffix}${stripSuffix}`;

    const overlayCount =
      this.state.au_overlays.length +
      this.state.nz_overlays.length +
      this.state.ca_overlays.length +
      this.state.us_overlays.length;
    const demShort = this.state.pack_id
      ? "Pack"
      : this.state.dem_source === "auto"
        ? "Auto"
        : this.state.dem_source === "au-elvis"
          ? "AU GA"
          : this.state.dem_source === "nz-linz"
            ? "NZ LINZ"
            : this.state.dem_source === "ca-hrdem"
              ? "CA HRDEM"
              : this.state.dem_source === "usgs3dep"
                ? "3DEP"
                : "GLO-30";
    this.overlayPillEl.textContent =
      overlayCount > 0 ? `${demShort} · +${overlayCount}` : demShort;

    const customCarto =
      this.state.carto.contour_density !== "normal" ||
      this.state.carto.hillshade_strength !== "normal" ||
      this.state.carto.label_density !== "normal" ||
      this.state.carto.show_slope_shading ||
      !this.state.carto.show_buildings ||
      !this.state.carto.show_trees ||
      !this.state.carto.show_power ||
      !this.state.carto.show_pipelines ||
      !this.state.carto.show_grid ||
      !this.state.carto.show_river_arrows ||
      this.state.overlayFile !== null;
    this.cartoPillEl.textContent = this.state.stripPlan
      ? "Strip"
      : this.state.carto.show_slope_shading
        ? "Slope"
        : customCarto
          ? "Custom"
          : "Default";
  }

  syncControlsFromState(clearPreset = false): void {
    if (clearPreset) {
      this.activePresetId = null;
    }
    this.centerInput.value = this.state.center;
    this.titleInput.value = this.state.title;
    this.scaleSelect.value = String(this.state.scale);
    this.paperSelect.value = this.state.paper;
    this.foldMarksCheck.checked = this.state.show_fold_marks;
    this.sheetAtlasIndexCheck.checked = this.state.include_atlas_index;
    this.setSegmentedValue("orientation", this.state.orientation);
    this.setSegmentedValue("color_mode", this.state.color_mode);
    this.setSegmentedValue("units", this.state.units);
    this.demSelect.value = this.state.dem_source;
    this.packSelect.value = this.state.pack_id;

    this.syncStateTrailControls();

    this.root
      .querySelectorAll<HTMLInputElement>("[data-au-overlay]")
      .forEach((cb) => {
        cb.checked = this.state.au_overlays.includes(
          cb.dataset.auOverlay as AUOverlayKind
        );
      });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-nz-overlay]")
      .forEach((cb) => {
        cb.checked = this.state.nz_overlays.includes(
          cb.dataset.nzOverlay as NZOverlayKind
        );
      });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-ca-overlay]")
      .forEach((cb) => {
        cb.checked = this.state.ca_overlays.includes(
          cb.dataset.caOverlay as CAOverlayKind
        );
      });

    this.root
      .querySelectorAll<HTMLInputElement>("[data-us-overlay]")
      .forEach((cb) => {
        cb.checked = this.state.us_overlays.includes(
          cb.dataset.usOverlay as USOverlayKind
        );
      });

    this.contourSelect.value = this.state.carto.contour_density;
    this.hillshadeSelect.value = this.state.carto.hillshade_strength;
    this.labelDensitySelect.value = this.state.carto.label_density;
    this.slopeCheck.checked = this.state.carto.show_slope_shading;

    this.root
      .querySelectorAll<HTMLInputElement>("[data-carto-toggle]")
      .forEach((cb) => {
        const key = cb.dataset.cartoToggle as keyof typeof this.state.carto;
        if (typeof this.state.carto[key] === "boolean") {
          cb.checked = this.state.carto[key] as boolean;
        }
      });

    this.vsDefObsInput.value = String(
      this.state.viewshedDefaults.observer_height_m
    );
    this.vsDefTgtInput.value = String(
      this.state.viewshedDefaults.target_height_m
    );
    this.vsDefRadInput.value = String(
      this.state.viewshedDefaults.max_distance_km
    );

    this.rfFreqInput.value = String(this.state.rfLinkDefaults.freq_mhz);
    this.rfTxHInput.value = String(this.state.rfLinkDefaults.from_height_m);
    this.rfRxHInput.value = String(this.state.rfLinkDefaults.to_height_m);
    const bandMatch = RF_BAND_PRESETS.find(
      (b) => Math.abs(b.freq_mhz - this.state.rfLinkDefaults.freq_mhz) < 0.05
    );
    this.rfBandSelect.value = bandMatch ? String(bandMatch.freq_mhz) : "custom";

    const o = this.state.outputs;
    this.outPdfCheck.checked = o.pdf;
    this.outPdfDpiSelect.value = String(this.state.dpi);
    this.outGeoPdfCheck.checked = o.geopdf;
    this.outCalibrationCheck.checked = this.state.include_calibration_page;
    this.outRouteCardCheck.checked = this.state.include_route_card;
    this.wpRouteCardCheck.checked = this.state.include_route_card;
    this.outElevProfileCheck.checked = this.state.include_elevation_profile;
    this.outAtlasIndexCheck.checked = this.state.include_atlas_index;
    this.outRfLinkCardCheck.checked = this.state.include_rf_link_card;
    this.rfCardCheck.checked = this.state.include_rf_link_card;

    this.outMbtilesCheck.checked = o.mbtiles;
    this.outMbtilesFmt.value = o.mbtiles_format;
    this.outKmzCheck.checked = o.kmz;
    this.outGarminProfile.value = o.garmin_profile;
    this.outGpkgCheck.checked = o.gpkg_tiles;
    this.outOsmandCheck.checked = o.osmand_sqlitedb;
    this.outBundleCheck.checked = o.field_bundle_zip;
    this.outDigitalGridCheck.checked = o.include_grid_on_digital;
    this.outExportDpiSelect.value = String(o.export_dpi);

    this.syncOutputLockStates();
    this.syncFileDisplay();
    this.renderRulerBox();
    this.renderSectorDraftBar();
    this.renderTacticalGraphicsList();
    this.renderWaypointsList();
    this.renderViewshedsList();
    this.renderRFLinksList();
    this.updateAtlasButtons();
    this.updatePresetButtons();
    this.updateSectionPills();
  }

  private syncOutputLockStates(): void {
    const o = this.state.outputs;
    const cap = this.session.capabilities;
    const hosted = this.session.auth_mode === "hosted";

    const applyTierLock = (
      input: HTMLInputElement,
      key: OutputKey,
      defaultTitle: string
    ): boolean => {
      const allowed = isOutputAllowed(key, cap);
      const wrap = input.closest<HTMLElement>(
        ".gf-output-line, .gf-inline-check, .gf-output-chip"
      );
      if (wrap) {
        wrap.classList.toggle("tier-locked", hosted && !allowed);
        if (hosted && !allowed) {
          wrap.title = "Friend invite required";
        } else if (defaultTitle) {
          wrap.title = defaultTitle;
        } else {
          wrap.removeAttribute("title");
        }
      }
      return allowed;
    };

    const pdfAllowed = applyTierLock(this.outPdfCheck, "pdf", "");
    const geopdfAllowed = applyTierLock(
      this.outGeoPdfCheck,
      "geopdf",
      "Embed geospatial reference tags in PDF"
    );
    const mbtilesAllowed = applyTierLock(this.outMbtilesCheck, "mbtiles", "");
    const kmzAllowed = applyTierLock(this.outKmzCheck, "kmz", "");
    const gpkgAllowed = applyTierLock(
      this.outGpkgCheck,
      "gpkg_tiles",
      "OGC GeoPackage Raster Tiles (.gpkg) for QGIS, AlpineQuest & Gaia"
    );
    const osmandAllowed = applyTierLock(
      this.outOsmandCheck,
      "osmand_sqlitedb",
      "OsmAnd / Locus Map BigPlanet SQLite (.sqlitedb)"
    );
    const bundleAllowed = applyTierLock(
      this.outBundleCheck,
      "field_bundle_zip",
      "Single Field Bundle (.zip) containing PDF, digital tiles, GPX 1.1 & README"
    );

    const pdfOn = o.pdf && pdfAllowed;
    const maxDpi = getEffectiveMaxDpi(cap);
    const maxExportDpi = getEffectiveMaxExportDpi(cap);
    this.outPdfCheck.disabled = !pdfAllowed;
    this.outPdfDpiSelect.disabled = !pdfOn;
    for (const opt of Array.from(this.outPdfDpiSelect.options)) {
      const val = Number(opt.value);
      const locked = Number.isFinite(val) && val > maxDpi;
      opt.disabled = locked;
      const baseOpt = DPI_OPTIONS.find((d) => d.value === val);
      opt.textContent = locked
        ? `${baseOpt?.shortLabel ?? `${val} DPI`} (Sign in)`
        : baseOpt?.shortLabel ?? `${val} DPI`;
    }
    this.outPdfDpiSelect.title =
      hosted && maxDpi < 600
        ? `Guest print resolution capped at ${maxDpi} DPI (sign in for 450/600 DPI)`
        : "Print PDF resolution (300 DPI standard; 450/600 DPI ultra-sharp hillshade)";
    this.outGeoPdfCheck.disabled = !pdfOn || !geopdfAllowed;
    this.outCalibrationCheck.disabled = !pdfOn;
    this.outRouteCardCheck.disabled = !pdfOn;
    this.outElevProfileCheck.disabled = !pdfOn;
    this.outAtlasIndexCheck.disabled = !pdfOn;
    this.outRfLinkCardCheck.disabled = !pdfOn;
    this.suppPagesBoxEl.style.opacity = pdfOn ? "1" : "0.45";
    if (!pdfOn || !geopdfAllowed) {
      o.geopdf = false;
      this.outGeoPdfCheck.checked = false;
    }

    this.outMbtilesCheck.disabled = !mbtilesAllowed;
    this.outMbtilesFmt.disabled = !mbtilesAllowed || !o.mbtiles;
    this.outKmzCheck.disabled = !kmzAllowed;
    this.outGarminProfile.disabled = !kmzAllowed || !o.kmz;
    this.outGpkgCheck.disabled = !gpkgAllowed;
    this.outOsmandCheck.disabled = !osmandAllowed;
    this.outBundleCheck.disabled = !bundleAllowed;

    const anyDigital =
      o.mbtiles || o.kmz || o.gpkg_tiles || o.osmand_sqlitedb;
    this.outDigitalGridCheck.disabled = !anyDigital;
    this.outExportDpiSelect.disabled = !anyDigital;
    for (const opt of Array.from(this.outExportDpiSelect.options)) {
      const val = Number(opt.value);
      const locked = Number.isFinite(val) && val > maxExportDpi;
      opt.disabled = locked;
      const baseOpt = DPI_OPTIONS.find((d) => d.value === val);
      opt.textContent = locked
        ? `${baseOpt?.shortLabel ?? `${val} DPI`} (Sign in)`
        : baseOpt?.shortLabel ?? `${val} DPI`;
    }
    this.outExportDpiSelect.title =
      hosted && maxExportDpi < 600
        ? `Guest export resolution capped at ${maxExportDpi} DPI (sign in for 450/600 DPI)`
        : "Digital export rasterization DPI (higher DPI generates sharper max zoom tiles)";
    this.digitalGridRow.style.opacity = anyDigital ? "1" : "0.45";
  }

  applySession(session: SessionResponse): void {
    this.session = session;
    this.maxUploadBytes =
      session.capabilities.max_upload_bytes || DEFAULT_MAX_UPLOAD_BYTES;
    const hosted = session.auth_mode === "hosted";
    this.managePacksBtn.hidden =
      hosted && !session.capabilities.packs_manage_allowed;

    this.syncFileDisplay();
    this.updateAtlasButtons();
    this.syncOutputLockStates();
    this.syncQuotaNoticeAndGenerateBtn();
  }

  getTurnstileContainer(): HTMLElement {
    return this.turnstileSlotEl;
  }

  private syncQuotaNoticeAndGenerateBtn(): void {
    const hosted = this.session.auth_mode === "hosted";
    const q = this.session.quotas;
    const paused = isGuestRenderPaused(this.session);
    const dailyExhausted =
      hosted &&
      q.daily_renders_remaining !== null &&
      q.daily_renders_remaining <= 0;
    const globalExhausted =
      hosted &&
      this.session.role === "guest" &&
      q.global_daily_public_limit !== null &&
      q.global_daily_public_used >= q.global_daily_public_limit;

    let noticeHtml = "";
    let blockedReason = "";

    if (paused) {
      noticeHtml =
        "Public renders are temporarily paused by the administrator.";
      blockedReason = noticeHtml;
    } else if (dailyExhausted) {
      if (this.session.role === "guest") {
        noticeHtml = `Daily guest render quota reached (0/${q.daily_renders_limit} left). <button type="button" class="gf-link-btn" data-action="open-auth">Sign in</button> for higher limits.`;
        blockedReason = "Daily guest render quota reached";
      } else {
        noticeHtml = `Daily render quota reached (0/${q.daily_renders_limit} left). Resets in 24h.`;
        blockedReason = "Daily render quota reached";
      }
    } else if (globalExhausted) {
      noticeHtml = `Global daily public render budget reached. <button type="button" class="gf-link-btn" data-action="open-auth">Sign in</button> or try again tomorrow.`;
      blockedReason = "Global daily public render budget reached";
    }

    if (noticeHtml) {
      this.quotaNoticeEl.innerHTML = noticeHtml;
      this.quotaNoticeEl.classList.remove("hidden");
    } else {
      this.quotaNoticeEl.innerHTML = "";
      this.quotaNoticeEl.classList.add("hidden");
    }

    this.generateBtn.disabled =
      this.isSubmitting || paused || dailyExhausted || globalExhausted;
    this.generateBtn.title = blockedReason;
  }

  setPlanningStrip(planning: boolean): void {
    this.planningStrip = planning;
    this.renderStripBar();
  }

  setInteractionMode(mode: MapInteractionMode, rfPendingFrom = false): void {
    this.interactionMode = mode;
    this.rfPendingFrom = rfPendingFrom;
    this.syncToolButtons();
    this.renderRulerBox();
    this.renderSectorDraftBar();
  }

  setRulerState(
    points: { lat: number; lon: number }[],
    metrics: RulerMetricsResponse | null,
    loading: boolean
  ): void {
    this.rulerPoints = points;
    this.rulerMetrics = metrics;
    this.rulerLoading = loading;
    this.renderRulerBox();
  }

  setSectorDraftCount(count: number): void {
    this.sectorDraftCount = count;
    this.renderSectorDraftBar();
  }

  setRouteMetrics(metrics: RouteMetricsResponse | null): void {
    this.routeMetrics = metrics;
    this.renderWaypointsList();
  }

  openWaypointsSection(): void {
    this.waypointsSectionEl.open = true;
  }

  openViewshedSection(): void {
    this.viewshedSectionEl.open = true;
  }

  resetWorkspace(): void {
    this.closeGeocodeMenu();
    this.fileInput.value = "";
    this.dropzoneEl.classList.remove("dragover");
    this.ringRadiiInput.value = this.ringRadiiInput.defaultValue;
    this.activePresetTab = "nsw-vic";
    this.activePresetId = "blue-mountains";
    this.presetTabsEl
      .querySelectorAll<HTMLButtonElement>("[data-preset-tab]")
      .forEach((b) =>
        b.classList.toggle("active", b.dataset.presetTab === this.activePresetTab)
      );
    this.renderPresetCards();
    this.setTitlePlaceholder("");
    this.setRouteMetrics(null);
    this.setRulerState([], null, false);
    this.setSectorDraftCount(0);
    this.setPacks([]);
    this.setPlanningStrip(false);
    this.setSubmitting(false);
    this.setFormError(null);
    this.syncControlsFromState();
  }

  setCenterInput(value: string, clearPreset = false): void {
    if (clearPreset) {
      this.activePresetId = null;
      this.updatePresetButtons();
    }
    this.centerInput.value = value;
  }

  setTitlePlaceholder(placeholder: string): void {
    this.titleInput.placeholder = placeholder || "Auto from MGRS / location";
  }

  setPacks(packs: PackSummary[]): void {
    const prev = this.state.pack_id;
    const options = [
      `<option value="">Cloud-Native On-Demand</option>`,
      ...packs.map((p) => {
        const meta = `${p.region} · ${p.vertical_datum} · ${formatBytes(p.size_bytes)}`;
        return `<option value="${escapeHtml(p.pack_id)}">${escapeHtml(p.name)} (${escapeHtml(meta)})</option>`;
      }),
    ];
    if (prev && !packs.some((p) => p.pack_id === prev)) {
      options.push(
        `<option value="${escapeHtml(prev)}">${escapeHtml(prev)} (unavailable)</option>`
      );
    }
    this.packSelect.innerHTML = options.join("");
    this.packSelect.value = prev;
    this.updateSectionPills();
  }

  setSubmitting(isSubmitting: boolean): void {
    this.isSubmitting = isSubmitting;
    this.generateBtn.innerHTML = isSubmitting
      ? `<span>Submitting Job…</span>`
      : `<span>Generate Map Sheet</span>`;
    this.syncQuotaNoticeAndGenerateBtn();
  }

  setFormError(message: string | null): void {
    if (!message) {
      this.formErrorEl.classList.add("hidden");
      this.formErrorEl.textContent = "";
    } else {
      this.formErrorEl.classList.remove("hidden");
      this.formErrorEl.textContent = message;
    }
  }

  focusCenterInput(): void {
    this.centerInput.focus();
    this.centerInput.select();
  }
}
