import { fetchGeocode } from "../api";
import {
  ATLAS_GRID_OPTIONS,
  AU_OVERLAY_OPTIONS,
  CARTO_TOGGLES,
  DEM_SOURCE_OPTIONS,
  DENSITY_OPTIONS,
  HILLSHADE_OPTIONS,
  LOCATION_PRESETS,
  PAPER_OPTIONS,
  SCALE_OPTIONS,
  US_OVERLAY_OPTIONS,
  escapeHtml,
  formatBytes,
  looksLikeCoordOrMgrs,
} from "../presets";
import type {
  AUOverlayKind,
  AppFormState,
  ColorMode,
  DemSourceKind,
  DensityLevel,
  GarminProfile,
  GeocodeResult,
  HillshadeStrength,
  LocationPreset,
  MBTilesFormat,
  Orientation,
  PackSummary,
  PaperSize,
  USOverlayKind,
  Units,
} from "../types";

export interface SidebarCallbacks {
  onCenterSubmit: (centerText: string) => void;
  onPlaceSelect: (place: GeocodeResult) => void;
  onPlaceNotFound: (query: string) => void;
  onPresetSelect: (preset: LocationPreset) => void;
  onFootprintParamsChange: (options?: { fitAfterLoad?: boolean }) => void;
  onStateChange: () => void;
  onRefreshPacks: () => void;
  onSubmitJob: () => void;
  onCopyCli: () => void;
}

const GEOCODE_DEBOUNCE_MS = 300;
const CHEVRON_SVG = `<svg class="gf-chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

export class SidebarPanel {
  private readonly root: HTMLElement;
  private readonly state: AppFormState;
  private readonly callbacks: SidebarCallbacks;

  private centerInput!: HTMLInputElement;
  private geocodeWrapEl!: HTMLElement;
  private geocodeMenuEl!: HTMLElement;
  private titleInput!: HTMLInputElement;
  private scaleSelect!: HTMLSelectElement;
  private paperSelect!: HTMLSelectElement;
  private atlasSublabelEl!: HTMLElement;
  private demSelect!: HTMLSelectElement;
  private packSelect!: HTMLSelectElement;
  private contourSelect!: HTMLSelectElement;
  private hillshadeSelect!: HTMLSelectElement;
  private labelDensitySelect!: HTMLSelectElement;
  private fileInput!: HTMLInputElement;
  private fileTitleEl!: HTMLElement;
  private fileSubEl!: HTMLElement;
  private fileClearBtn!: HTMLButtonElement;
  private dropzoneEl!: HTMLElement;
  private outPdfCheck!: HTMLInputElement;
  private outGeoPdfCheck!: HTMLInputElement;
  private outMbtilesCheck!: HTMLInputElement;
  private outMbtilesFmt!: HTMLSelectElement;
  private outKmzCheck!: HTMLInputElement;
  private outGarminProfile!: HTMLSelectElement;
  private outDigitalGridCheck!: HTMLInputElement;
  private digitalGridRow!: HTMLElement;
  private generateBtn!: HTMLButtonElement;
  private formErrorEl!: HTMLElement;
  private sheetPillEl!: HTMLElement;
  private overlayPillEl!: HTMLElement;
  private cartoPillEl!: HTMLElement;
  private activePresetId: string | null = "blue-mountains";

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
    this.syncControlsFromState();
  }

  private render(): void {
    const presetsHtml = LOCATION_PRESETS.map(
      (p) => `
        <button type="button" class="gf-preset-btn" data-preset="${escapeHtml(p.id)}">
          <span class="gf-preset-name">${escapeHtml(p.label)}</span>
          <span class="gf-preset-tag">${escapeHtml(p.regionTag)} · 1:${(p.scale / 1000).toFixed(0)}k</span>
        </button>
      `
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

    const demOptsHtml = DEM_SOURCE_OPTIONS.map(
      (d) => `<option value="${d.value}">${escapeHtml(d.label)}</option>`
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

    const cartoTogglesHtml = CARTO_TOGGLES.map(
      (t) => `
        <label class="gf-toggle-chip">
          <input type="checkbox" data-carto-toggle="${t.key}" />
          <span>${escapeHtml(t.label)}</span>
        </label>
      `
    ).join("");

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
              <span class="gf-label">Quick-Jump Presets</span>
              <div class="gf-preset-grid">
                ${presetsHtml}
              </div>
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
                <span class="gf-label">Color Mode</span>
                <div class="gf-segmented" data-seg="color_mode">
                  <button type="button" class="gf-seg-btn" data-value="color">Color</button>
                  <button type="button" class="gf-seg-btn" data-value="bw">B&amp;W Print</button>
                </div>
              </div>
            </div>

            <div class="gf-field">
              <span class="gf-label">Elevation &amp; Scale Bar Units</span>
              <div class="gf-segmented" data-seg="units">
                <button type="button" class="gf-seg-btn" data-value="metric">Metric (m / km)</button>
                <button type="button" class="gf-seg-btn" data-value="imperial">Imperial (ft / mi)</button>
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
                <button type="button" class="gf-mini-btn" data-role="refresh-packs" title="Reload installed packs">↻</button>
              </label>
              <select id="gf-pack-select" class="gf-select">
                <option value="">Cloud-Native On-Demand</option>
              </select>
            </div>

            <div class="gf-overlay-group">
              <span class="gf-overlay-heading">Australia National Overlays (CC BY 4.0)</span>
              ${auOverlaysHtml}
            </div>

            <div class="gf-overlay-group">
              <span class="gf-overlay-heading">North America Overlays</span>
              ${usOverlaysHtml}
            </div>
          </div>
        </details>

        <!-- 4. Cartography & User Overlay -->
        <details class="gf-section">
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
            </div>

            <div class="gf-field">
              <span class="gf-label">
                <span>Custom User Overlay</span>
                <span class="gf-sublabel">.geojson · .gpx · .csv</span>
              </span>
              <div class="gf-dropzone" data-role="dropzone">
                <label class="gf-dropzone-label" for="gf-overlay-file">
                  <span class="gf-dropzone-title" data-role="file-title">Drop or browse overlay file…</span>
                  <span class="gf-dropzone-sub" data-role="file-sub">Waypoints, tracks, or polygons (max 25 MB)</span>
                </label>
                <input
                  id="gf-overlay-file"
                  type="file"
                  accept=".geojson,.json,.gpx,.csv"
                  hidden
                />
                <button type="button" class="gf-file-clear" data-role="file-clear" title="Clear overlay file" hidden>×</button>
              </div>
            </div>
          </div>
        </details>
      </div>

      <!-- 5. Outputs & Generate Sticky Footer -->
      <div class="gf-sidebar-footer">
        <div class="gf-outputs-box">
          <div class="gf-output-line">
            <label class="gf-output-check">
              <input type="checkbox" data-role="out-pdf" />
              <span>Print PDF (300 DPI)</span>
            </label>
            <label class="gf-inline-check" title="Embed geospatial reference tags in PDF">
              <input type="checkbox" data-role="out-geopdf" />
              <span>GeoPDF</span>
            </label>
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

          <label class="gf-inline-check" data-role="digital-grid-row" style="padding: 2px 4px;">
            <input type="checkbox" data-role="out-digital-grid" />
            <span>Include MGRS Grid on Digital Exports</span>
          </label>
        </div>

        <div class="gf-form-error hidden" data-role="form-error"></div>

        <div class="gf-footer-actions">
          <button type="button" class="gf-generate-btn" data-role="generate-btn">
            <span>Generate Map Sheet</span>
          </button>
          <button
            type="button"
            class="gf-cli-btn"
            data-role="copy-cli"
            title="Copy reproducible docker compose CLI command"
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
    this.titleInput = this.q<HTMLInputElement>("#gf-title-input");
    this.scaleSelect = this.q<HTMLSelectElement>("#gf-scale-select");
    this.paperSelect = this.q<HTMLSelectElement>("#gf-paper-select");
    this.atlasSublabelEl = this.q("[data-role='atlas-sublabel']");
    this.demSelect = this.q<HTMLSelectElement>("#gf-dem-select");
    this.packSelect = this.q<HTMLSelectElement>("#gf-pack-select");
    this.contourSelect = this.q<HTMLSelectElement>("#gf-contour-select");
    this.hillshadeSelect = this.q<HTMLSelectElement>("#gf-hillshade-select");
    this.labelDensitySelect = this.q<HTMLSelectElement>("#gf-label-select");
    this.fileInput = this.q<HTMLInputElement>("#gf-overlay-file");
    this.fileTitleEl = this.q("[data-role='file-title']");
    this.fileSubEl = this.q("[data-role='file-sub']");
    this.fileClearBtn = this.q<HTMLButtonElement>("[data-role='file-clear']");
    this.dropzoneEl = this.q("[data-role='dropzone']");
    this.outPdfCheck = this.q<HTMLInputElement>("[data-role='out-pdf']");
    this.outGeoPdfCheck = this.q<HTMLInputElement>("[data-role='out-geopdf']");
    this.outMbtilesCheck = this.q<HTMLInputElement>("[data-role='out-mbtiles']");
    this.outMbtilesFmt = this.q<HTMLSelectElement>(
      "[data-role='out-mbtiles-fmt']"
    );
    this.outKmzCheck = this.q<HTMLInputElement>("[data-role='out-kmz']");
    this.outGarminProfile = this.q<HTMLSelectElement>(
      "[data-role='out-garmin-profile']"
    );
    this.outDigitalGridCheck = this.q<HTMLInputElement>(
      "[data-role='out-digital-grid']"
    );
    this.digitalGridRow = this.q("[data-role='digital-grid-row']");
    this.generateBtn = this.q<HTMLButtonElement>("[data-role='generate-btn']");
    this.formErrorEl = this.q("[data-role='form-error']");
    this.sheetPillEl = this.q("[data-role='sheet-pill']");
    this.overlayPillEl = this.q("[data-role='overlay-pill']");
    this.cartoPillEl = this.q("[data-role='carto-pill']");
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Sidebar element not found: ${selector}`);
    return el;
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
        this.callbacks.onCenterSubmit(value);
        return;
      }

      if (this.geocodeResults.length > 0 && this.geocodeResults[0]) {
        this.choosePlace(this.geocodeResults[0]);
        return;
      }

      // Immediate place lookup on Enter/Go
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
      } catch {
        if (!controller.signal.aborted) {
          this.closeGeocodeMenu();
          this.callbacks.onPlaceNotFound(value);
        }
      }
    };

    this.q("[data-role='go-center']").addEventListener("click", () => {
      void submitCenterOrPlace();
    });

    this.centerInput.addEventListener("input", () => {
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
      // Prevent input blur before click registers
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

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-preset]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const preset = LOCATION_PRESETS.find(
            (p) => p.id === btn.dataset.preset
          );
          if (!preset) return;
          this.closeGeocodeMenu();
          this.activePresetId = preset.id;
          this.callbacks.onPresetSelect(preset);
        });
      });

    this.titleInput.addEventListener("input", () => {
      this.state.title = this.titleInput.value;
      this.callbacks.onStateChange();
    });

    this.scaleSelect.addEventListener("change", () => {
      this.state.scale = Number(this.scaleSelect.value);
      this.updateSectionPills();
      this.callbacks.onFootprintParamsChange();
    });

    this.paperSelect.addEventListener("change", () => {
      this.state.paper = this.paperSelect.value as PaperSize;
      this.updateSectionPills();
      this.callbacks.onFootprintParamsChange();
    });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-atlas-cols]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const cols = Number(btn.dataset.atlasCols);
          const rows = Number(btn.dataset.atlasRows);
          if (
            this.state.grid_cols === cols &&
            this.state.grid_rows === rows
          ) {
            return;
          }
          this.state.grid_cols = cols;
          this.state.grid_rows = rows;
          this.updateAtlasButtons();
          this.updateSectionPills();
          this.callbacks.onFootprintParamsChange({ fitAfterLoad: true });
        });
      });

    this.bindSegmented("orientation", (val) => {
      this.state.orientation = val as Orientation;
      this.updateSectionPills();
      this.callbacks.onFootprintParamsChange();
    });

    this.bindSegmented("color_mode", (val) => {
      this.state.color_mode = val as ColorMode;
      this.callbacks.onStateChange();
    });

    this.bindSegmented("units", (val) => {
      this.state.units = val as Units;
      this.callbacks.onFootprintParamsChange();
    });

    this.demSelect.addEventListener("change", () => {
      this.state.dem_source = this.demSelect.value as DemSourceKind;
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.packSelect.addEventListener("change", () => {
      this.state.pack_id = this.packSelect.value;
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.q("[data-role='refresh-packs']").addEventListener("click", () => {
      this.callbacks.onRefreshPacks();
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
          this.updateSectionPills();
          this.callbacks.onStateChange();
        });
      });

    this.contourSelect.addEventListener("change", () => {
      this.state.carto.contour_density = this.contourSelect
        .value as DensityLevel;
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.hillshadeSelect.addEventListener("change", () => {
      this.state.carto.hillshade_strength = this.hillshadeSelect
        .value as HillshadeStrength;
      this.updateSectionPills();
      this.callbacks.onStateChange();
    });

    this.labelDensitySelect.addEventListener("change", () => {
      this.state.carto.label_density = this.labelDensitySelect
        .value as DensityLevel;
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
            this.updateSectionPills();
            this.callbacks.onStateChange();
          }
        });
      });

    const handleFile = (file: File | null) => {
      if (file && file.size > 25 * 1024 * 1024) {
        this.setFormError("Overlay file exceeds 25 MB limit.");
        return;
      }
      this.setFormError(null);
      this.state.overlayFile = file;
      this.syncFileDisplay();
      this.updateSectionPills();
      this.callbacks.onStateChange();
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

    // Outputs
    const syncOutputControls = () => {
      this.outGeoPdfCheck.disabled = !this.state.outputs.pdf;
      if (!this.state.outputs.pdf) {
        this.state.outputs.geopdf = false;
        this.outGeoPdfCheck.checked = false;
      }
      this.outMbtilesFmt.disabled = !this.state.outputs.mbtiles;
      this.outGarminProfile.disabled = !this.state.outputs.kmz;
      const anyDigital = this.state.outputs.mbtiles || this.state.outputs.kmz;
      this.outDigitalGridCheck.disabled = !anyDigital;
      this.digitalGridRow.style.opacity = anyDigital ? "1" : "0.45";

      const anyOutput =
        this.state.outputs.pdf ||
        this.state.outputs.mbtiles ||
        this.state.outputs.kmz;
      if (!anyOutput) {
        this.setFormError("Select at least one output (PDF, MBTiles, or KMZ).");
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
      this.callbacks.onStateChange();
    });

    this.outMbtilesCheck.addEventListener("change", () => {
      this.state.outputs.mbtiles = this.outMbtilesCheck.checked;
      syncOutputControls();
      this.callbacks.onStateChange();
    });

    this.outMbtilesFmt.addEventListener("change", () => {
      this.state.outputs.mbtiles_format = this.outMbtilesFmt
        .value as MBTilesFormat;
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
      this.callbacks.onStateChange();
    });

    this.outDigitalGridCheck.addEventListener("change", () => {
      this.state.outputs.include_grid_on_digital =
        this.outDigitalGridCheck.checked;
      this.callbacks.onStateChange();
    });

    this.generateBtn.addEventListener("click", () => {
      const anyOutput =
        this.state.outputs.pdf ||
        this.state.outputs.mbtiles ||
        this.state.outputs.kmz;
      if (!anyOutput) {
        this.setFormError("Select at least one output (PDF, MBTiles, or KMZ).");
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
    } catch {
      if (!controller.signal.aborted) {
        this.closeGeocodeMenu();
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
      .querySelectorAll<HTMLButtonElement>(".gf-seg-btn")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const val = btn.dataset.value;
          if (!val) return;
          group
            .querySelectorAll<HTMLButtonElement>(".gf-seg-btn")
            .forEach((b) => b.classList.toggle("active", b === btn));
          onChange(val);
        });
      });
  }

  private setSegmentedValue(name: string, value: string): void {
    const group = this.q(`[data-seg='${name}']`);
    group
      .querySelectorAll<HTMLButtonElement>(".gf-seg-btn")
      .forEach((b) => b.classList.toggle("active", b.dataset.value === value));
  }

  private updateAtlasButtons(): void {
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-atlas-cols]")
      .forEach((btn) => {
        const active =
          Number(btn.dataset.atlasCols) === this.state.grid_cols &&
          Number(btn.dataset.atlasRows) === this.state.grid_rows;
        btn.classList.toggle("active", active);
      });
    const count = this.state.grid_cols * this.state.grid_rows;
    this.atlasSublabelEl.textContent =
      count === 1
        ? "Single sheet"
        : `${count} edge-matched sheets (multi-page PDF)`;
  }

  private updatePresetButtons(): void {
    this.root
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
      this.fileSubEl.textContent = "Waypoints, tracks, or polygons (max 25 MB)";
      this.fileClearBtn.hidden = true;
    }
  }

  private updateSectionPills(): void {
    const scaleK = `1:${Math.round(this.state.scale / 1000)}k`;
    const paperLbl = this.state.paper.toUpperCase();
    const orientShort = this.state.orientation === "landscape" ? "L" : "P";
    const atlasSuffix =
      this.state.grid_cols * this.state.grid_rows > 1
        ? ` · ${this.state.grid_cols}×${this.state.grid_rows}`
        : "";
    this.sheetPillEl.textContent = `${scaleK} · ${paperLbl} ${orientShort}${atlasSuffix}`;

    const overlayCount =
      this.state.au_overlays.length + this.state.us_overlays.length;
    const demShort = this.state.pack_id
      ? "Pack"
      : this.state.dem_source === "auto"
        ? "Auto"
        : this.state.dem_source === "au-elvis"
          ? "AU GA"
          : this.state.dem_source === "usgs3dep"
            ? "3DEP"
            : "GLO-30";
    this.overlayPillEl.textContent =
      overlayCount > 0 ? `${demShort} · +${overlayCount}` : demShort;

    const customCarto =
      this.state.carto.contour_density !== "normal" ||
      this.state.carto.hillshade_strength !== "normal" ||
      this.state.carto.label_density !== "normal" ||
      !this.state.carto.show_buildings ||
      !this.state.carto.show_trees ||
      !this.state.carto.show_power ||
      !this.state.carto.show_pipelines ||
      !this.state.carto.show_grid ||
      !this.state.carto.show_river_arrows ||
      this.state.overlayFile !== null;
    this.cartoPillEl.textContent = customCarto ? "Custom" : "Default";
  }

  syncControlsFromState(clearPreset = false): void {
    if (clearPreset) {
      this.activePresetId = null;
    }
    this.centerInput.value = this.state.center;
    this.titleInput.value = this.state.title;
    this.scaleSelect.value = String(this.state.scale);
    this.paperSelect.value = this.state.paper;
    this.setSegmentedValue("orientation", this.state.orientation);
    this.setSegmentedValue("color_mode", this.state.color_mode);
    this.setSegmentedValue("units", this.state.units);
    this.demSelect.value = this.state.dem_source;
    this.packSelect.value = this.state.pack_id;

    this.root
      .querySelectorAll<HTMLInputElement>("[data-au-overlay]")
      .forEach((cb) => {
        cb.checked = this.state.au_overlays.includes(
          cb.dataset.auOverlay as AUOverlayKind
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

    this.root
      .querySelectorAll<HTMLInputElement>("[data-carto-toggle]")
      .forEach((cb) => {
        const key = cb.dataset.cartoToggle as keyof typeof this.state.carto;
        if (typeof this.state.carto[key] === "boolean") {
          cb.checked = this.state.carto[key] as boolean;
        }
      });

    this.outPdfCheck.checked = this.state.outputs.pdf;
    this.outGeoPdfCheck.checked = this.state.outputs.geopdf;
    this.outGeoPdfCheck.disabled = !this.state.outputs.pdf;
    this.outMbtilesCheck.checked = this.state.outputs.mbtiles;
    this.outMbtilesFmt.value = this.state.outputs.mbtiles_format;
    this.outMbtilesFmt.disabled = !this.state.outputs.mbtiles;
    this.outKmzCheck.checked = this.state.outputs.kmz;
    this.outGarminProfile.value = this.state.outputs.garmin_profile;
    this.outGarminProfile.disabled = !this.state.outputs.kmz;
    this.outDigitalGridCheck.checked =
      this.state.outputs.include_grid_on_digital;
    const anyDigital = this.state.outputs.mbtiles || this.state.outputs.kmz;
    this.outDigitalGridCheck.disabled = !anyDigital;
    this.digitalGridRow.style.opacity = anyDigital ? "1" : "0.45";

    this.syncFileDisplay();
    this.updateAtlasButtons();
    this.updatePresetButtons();
    this.updateSectionPills();
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
    this.packSelect.innerHTML = options.join("");
    if (prev && packs.some((p) => p.pack_id === prev)) {
      this.packSelect.value = prev;
    } else {
      this.state.pack_id = "";
      this.packSelect.value = "";
    }
    this.updateSectionPills();
  }

  setSubmitting(isSubmitting: boolean): void {
    this.generateBtn.disabled = isSubmitting;
    this.generateBtn.innerHTML = isSubmitting
      ? `<span>Submitting Job…</span>`
      : `<span>Generate Map Sheet</span>`;
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
