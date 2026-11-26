import "./styles.css";
import {
  cancelJob,
  deleteJob,
  fetchFootprint,
  fetchHealth,
  fetchJobs,
  fetchPacks,
  fetchRouteMetrics,
  fetchStripPlan,
  submitJob,
} from "./api";
import { JobsDrawer } from "./components/jobsDrawer";
import { SidebarPanel } from "./components/sidebar";
import { TelemetryBar } from "./components/telemetry";
import { GridForgeMap } from "./map";
import {
  ALL_AU_OVERLAY_KINDS,
  BASEMAP_OPTIONS,
  DEM_SOURCE_OPTIONS,
  PAPER_OPTIONS,
  SCALE_OPTIONS,
  US_OVERLAY_OPTIONS,
  buildCliCommand,
  createInitialFormState,
  escapeHtml,
  formatCoords,
  parseLatLon,
} from "./presets";
import type {
  AUOverlayKind,
  AppFormState,
  BasemapId,
  ColorMode,
  CreateJobPayload,
  DemSourceKind,
  FootprintResponse,
  GeocodeResult,
  JobRecord,
  LocationPreset,
  Orientation,
  PaperSize,
  USOverlayKind,
  Units,
  WaypointCategory,
} from "./types";

const FOOTPRINT_DEBOUNCE_MS = 180;
const ROUTE_METRICS_DEBOUNCE_MS = 150;
const ACTIVE_JOBS_POLL_MS = 2000;
const IDLE_JOBS_POLL_MS = 10000;
const HEALTH_POLL_MS = 5000;

const VALID_SCALES = new Set(SCALE_OPTIONS.map((s) => s.value));
const VALID_PAPERS = new Set<string>(PAPER_OPTIONS.map((p) => p.value));
const VALID_DEMS = new Set<string>(DEM_SOURCE_OPTIONS.map((d) => d.value));
const VALID_AU_OV = new Set<string>(ALL_AU_OVERLAY_KINDS);
const VALID_US_OV = new Set<string>(US_OVERLAY_OPTIONS.map((o) => o.value));

function hydrateStateFromUrl(state: AppFormState): {
  hasCustomCenter: boolean;
  needsFlyToResolvedCenter: boolean;
} {
  const params = new URLSearchParams(window.location.search);
  let hasCustomCenter = false;
  let needsFlyToResolvedCenter = false;

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
  if (modeParam === "color" || modeParam === "bw") {
    state.color_mode = modeParam as ColorMode;
  }

  const unitsParam = params.get("units")?.toLowerCase();
  if (unitsParam === "metric" || unitsParam === "imperial") {
    state.units = unitsParam as Units;
  }

  const demParam = params.get("dem")?.toLowerCase();
  if (demParam && VALID_DEMS.has(demParam)) {
    state.dem_source = demParam as DemSourceKind;
  }

  const gridParam = params.get("grid")?.toLowerCase();
  if (gridParam) {
    const match = /^([1-3])x([1-3])$/.exec(gridParam);
    if (match) {
      state.grid_cols = Number(match[1]);
      state.grid_rows = Number(match[2]);
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

  const usParam = params.get("us")?.trim();
  if (usParam) {
    state.us_overlays = usParam
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s): s is USOverlayKind => VALID_US_OV.has(s));
  }

  if (params.get("slope") === "1" || params.get("slope") === "true") {
    state.carto.show_slope_shading = true;
  }

  if (params.get("cal") === "1") {
    state.include_calibration_page = true;
  }

  return { hasCustomCenter, needsFlyToResolvedCenter };
}

class GridForgeApp {
  private readonly state = createInitialFormState();
  private readonly shellEl: HTMLElement;
  private readonly telemetry: TelemetryBar;
  private readonly sidebar: SidebarPanel;
  private readonly drawer: JobsDrawer;
  private readonly map: GridForgeMap;
  private readonly footprintBannerEl: HTMLElement;
  private readonly mapHintEl: HTMLElement;
  private readonly mapHintTextEl: HTMLElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly collapseBtnEl: HTMLButtonElement;
  private readonly markupToolbarBtn: HTMLButtonElement;

  private sidebarCollapsed = false;
  private drawerOpen = false;
  private markupMode = false;
  private activeBasemap: BasemapId = "opentopo";
  private latestFootprint: FootprintResponse | null = null;
  private jobs: JobRecord[] = [];
  private readonly initialFlyToResolved: boolean;

  private footprintTimer: number | null = null;
  private footprintAbort: AbortController | null = null;
  private stripAbort: AbortController | null = null;
  private metricsTimer: number | null = null;
  private metricsAbort: AbortController | null = null;
  private jobsPollTimer: number | null = null;

  constructor(root: HTMLElement) {
    const urlHydration = hydrateStateFromUrl(this.state);
    this.initialFlyToResolved = urlHydration.needsFlyToResolvedCenter;

    root.innerHTML = `
      <div class="gf-shell" data-role="shell">
        <header data-role="topbar"></header>
        <main class="gf-workspace">
          <div class="gf-map-container" data-role="map"></div>

          <button
            type="button"
            class="gf-sidebar-collapse-btn"
            data-role="sidebar-toggle"
            title="Toggle studio sidebar"
            aria-label="Toggle studio sidebar"
          >◂</button>

          <aside data-role="sidebar"></aside>

          <div class="gf-map-toolbar">
            <div class="gf-basemap-switcher" role="group" aria-label="Basemap switcher">
              ${BASEMAP_OPTIONS.map(
                (b) =>
                  `<button type="button" class="gf-basemap-btn ${b.id === this.activeBasemap ? "active" : ""}" data-basemap="${escapeHtml(b.id)}">${escapeHtml(b.label)}</button>`
              ).join("")}
            </div>
            <div class="gf-map-actions">
              <button
                type="button"
                class="gf-map-icon-btn gf-markup-btn"
                data-map-action="markup"
                aria-pressed="false"
                title="Toggle waypoint markup drop mode (M)"
              >⌖ Markup</button>
              <button type="button" class="gf-map-icon-btn" data-map-action="fit" title="Fit sheet footprint (F)">⌖ Fit</button>
              <button type="button" class="gf-map-icon-btn" data-map-action="zoom-in" title="Zoom in">+</button>
              <button type="button" class="gf-map-icon-btn" data-map-action="zoom-out" title="Zoom out">−</button>
              <button type="button" class="gf-map-icon-btn" data-map-action="north" title="Reset north">N</button>
            </div>
          </div>

          <div class="gf-footprint-banner hidden" data-role="footprint-banner"></div>

          <div class="gf-map-hint" data-role="map-hint">
            <span data-role="map-hint-text">Click map or drag reticle · F to fit</span>
            <span class="mono" data-role="cursor-coords">—</span>
          </div>

          <aside data-role="drawer"></aside>
        </main>
        <div class="gf-toast-container" data-role="toasts"></div>
      </div>
    `;

    this.shellEl = this.q(root, "[data-role='shell']");
    this.footprintBannerEl = this.q(root, "[data-role='footprint-banner']");
    this.mapHintEl = this.q(root, "[data-role='map-hint']");
    this.mapHintTextEl = this.q(root, "[data-role='map-hint-text']");
    this.cursorCoordsEl = this.q(root, "[data-role='cursor-coords']");
    this.toastContainerEl = this.q(root, "[data-role='toasts']");
    this.collapseBtnEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='sidebar-toggle']"
    );
    this.markupToolbarBtn = this.q<HTMLButtonElement>(
      root,
      "[data-map-action='markup']"
    );

    this.telemetry = new TelemetryBar(this.q(root, "[data-role='topbar']"), {
      onToggleDrawer: () => this.setDrawerOpen(!this.drawerOpen),
      onCopyMgrs: (mgrs) => this.copyText(mgrs, `Copied ${mgrs}`),
    });

    this.sidebar = new SidebarPanel(
      this.q(root, "[data-role='sidebar']"),
      this.state,
      {
        onCenterSubmit: (centerText) => {
          this.clearStripPlanInternal(false);
          this.state.center = centerText;
          this.syncUrlFromState();
          this.requestFootprintImmediate({
            flyToResult: true,
            syncMarker: true,
          });
        },
        onPlaceSelect: (place) => this.applyPlaceResult(place),
        onPlaceNotFound: (query) =>
          this.showToast(`No place found for "${query}"`, "error"),
        onPresetSelect: (preset) => this.applyPreset(preset),
        onFootprintParamsChange: (opts) => {
          this.syncUrlFromState();
          if (this.state.stripPlan && this.state.overlayFile) {
            void this.runStripPlan({ fitAfterLoad: true, silentToast: true });
          } else if (opts?.fitAfterLoad) {
            this.requestFootprintImmediate({
              flyToResult: false,
              syncMarker: false,
              fitAfterLoad: true,
            });
          } else {
            this.scheduleFootprint();
          }
          if (this.state.waypoints.length > 0) {
            this.scheduleRouteMetrics();
          }
        },
        onStateChange: () => {
          this.syncUrlFromState();
          if (this.state.stripPlan) {
            this.map.updateFootprint(
              this.state.stripPlan.footprint_geojson,
              this.state.stripPlan.grid_geojson,
              this.state.carto.show_grid
            );
          } else if (this.latestFootprint) {
            this.map.updateFootprint(
              this.latestFootprint.footprint_geojson,
              this.latestFootprint.grid_geojson,
              this.state.carto.show_grid
            );
          }
        },
        onOverlayFileChange: (file) => {
          if (!file && this.state.stripPlan) {
            this.clearStripPlanInternal(true);
          } else if (this.state.stripPlan && file) {
            this.clearStripPlanInternal(true);
          }
        },
        onAutoStripPlan: () =>
          void this.runStripPlan({ fitAfterLoad: true, silentToast: false }),
        onClearStripPlan: () => this.clearStripPlanInternal(true),
        onToggleMarkupMode: () => this.setMarkupMode(!this.markupMode),
        onWaypointEdit: (idx, patch) => this.handleWaypointEdit(idx, patch),
        onWaypointDelete: (idx) => this.handleWaypointDelete(idx),
        onWaypointsClear: () => this.handleWaypointsClear(),
        onRefreshPacks: () => void this.loadPacks(true),
        onSubmitJob: () => void this.handleSubmitJob(),
        onCopyCli: () => {
          const cmd = buildCliCommand(this.state);
          this.copyText(cmd, "Copied CLI command!");
        },
      },
      urlHydration.hasCustomCenter ? null : "blue-mountains"
    );

    this.drawer = new JobsDrawer(this.q(root, "[data-role='drawer']"), {
      onClose: () => this.setDrawerOpen(false),
      onRefresh: () => void this.pollJobsOnce(),
      onCancelJob: (jobId) => void this.handleCancelJob(jobId),
      onDeleteJob: (jobId) => void this.handleDeleteJob(jobId),
    });

    this.map = new GridForgeMap(
      this.q(root, "[data-role='map']"),
      {
        onCenterDrag: (lat, lon) => {
          if (this.state.stripPlan) {
            this.clearStripPlanInternal(false);
          }
          this.state.lat = lat;
          this.state.lon = lon;
          this.state.center = formatCoords(lat, lon);
          this.sidebar.setCenterInput(this.state.center, true);
          this.scheduleFootprint();
        },
        onCenterCommit: (lat, lon) => {
          if (this.state.stripPlan) {
            this.clearStripPlanInternal(false);
          }
          this.state.lat = lat;
          this.state.lon = lon;
          this.state.center = formatCoords(lat, lon);
          this.sidebar.setCenterInput(this.state.center, true);
          this.syncUrlFromState();
          this.scheduleFootprint();
        },
        onMapMarkupClick: (lat, lon) => this.handleAddWaypoint(lat, lon),
        onCursorMove: (lat, lon) => {
          this.cursorCoordsEl.textContent = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
        },
      },
      {
        initialLat: this.state.lat,
        initialLon: this.state.lon,
      }
    );

    this.bindChromeEvents(root);
    this.bootstrap();
  }

  private q<T extends HTMLElement = HTMLElement>(
    root: ParentNode,
    selector: string
  ): T {
    const el = root.querySelector<T>(selector);
    if (!el) throw new Error(`App element not found: ${selector}`);
    return el;
  }

  private bindChromeEvents(root: HTMLElement): void {
    this.collapseBtnEl.addEventListener("click", () => {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      this.shellEl.classList.toggle("sidebar-collapsed", this.sidebarCollapsed);
      this.collapseBtnEl.textContent = this.sidebarCollapsed ? "▸" : "◂";
      window.setTimeout(() => this.map.resize(), 240);
    });

    root
      .querySelectorAll<HTMLButtonElement>("[data-basemap]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.basemap as BasemapId;
          this.activeBasemap = id;
          this.map.setBasemap(id);
          root
            .querySelectorAll<HTMLButtonElement>("[data-basemap]")
            .forEach((b) => b.classList.toggle("active", b === btn));
        });
      });

    root
      .querySelectorAll<HTMLButtonElement>("[data-map-action]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const action = btn.dataset.mapAction;
          if (action === "markup") {
            this.setMarkupMode(!this.markupMode);
          } else if (action === "fit") {
            this.fitSheetOnMap();
          } else if (action === "zoom-in") {
            this.map.zoomIn();
          } else if (action === "zoom-out") {
            this.map.zoomOut();
          } else if (action === "north") {
            this.map.resetNorth();
          }
        });
      });

    window.addEventListener("keydown", (e) => {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      const inInput =
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        Boolean(target?.isContentEditable);

      if (e.key === "/" && !inInput) {
        e.preventDefault();
        if (this.sidebarCollapsed) {
          this.collapseBtnEl.click();
        }
        this.sidebar.focusCenterInput();
      } else if (
        (e.key === "f" || e.key === "F") &&
        !inInput &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        this.fitSheetOnMap();
      } else if (
        (e.key === "m" || e.key === "M") &&
        !inInput &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        this.setMarkupMode(!this.markupMode);
      } else if (e.key === "Escape") {
        if (this.markupMode) {
          this.setMarkupMode(false);
        } else if (this.drawerOpen) {
          this.setDrawerOpen(false);
        }
      }
    });
  }

  private setMarkupMode(active: boolean): void {
    this.markupMode = active;
    this.map.setMarkupMode(active);
    this.sidebar.setMarkupMode(active);
    this.markupToolbarBtn.classList.toggle("active", active);
    this.markupToolbarBtn.setAttribute("aria-pressed", String(active));
    this.mapHintEl.classList.toggle("markup-active", active);
    this.mapHintTextEl.textContent = active
      ? "Markup ON: click map to drop WP · M to exit"
      : "Click map or drag reticle · F to fit";
    if (active) {
      this.sidebar.openWaypointsSection();
    }
  }

  private fitSheetOnMap(): void {
    this.map.fitFootprint(this.sidebarCollapsed ? 24 : 368);
  }

  private bootstrap(): void {
    this.requestFootprintImmediate({
      flyToResult: this.initialFlyToResolved,
      syncMarker: true,
      fitAfterLoad: this.state.grid_cols * this.state.grid_rows > 1,
    });
    void this.loadHealth();
    void this.loadPacks(false);
    void this.pollJobsOnce();

    window.setInterval(() => void this.loadHealth(), HEALTH_POLL_MS);
    window.setInterval(() => this.drawer.tickElapsed(), 1000);
  }

  private syncUrlFromState(): void {
    const params = new URLSearchParams();
    params.set("center", this.state.center.replace(/\s*,\s*/, ","));
    params.set("scale", String(this.state.scale));
    params.set("paper", this.state.paper);
    params.set("orientation", this.state.orientation);
    params.set("mode", this.state.color_mode);
    params.set("units", this.state.units);
    if (this.state.dem_source !== "auto") {
      params.set("dem", this.state.dem_source);
    }
    if (this.state.grid_cols !== 1 || this.state.grid_rows !== 1) {
      params.set("grid", `${this.state.grid_cols}x${this.state.grid_rows}`);
    }
    if (this.state.title.trim()) {
      params.set("title", this.state.title.trim());
    }
    if (this.state.au_overlays.length > 0) {
      params.set("au", this.state.au_overlays.join(","));
    }
    if (this.state.us_overlays.length > 0) {
      params.set("us", this.state.us_overlays.join(","));
    }
    if (this.state.carto.show_slope_shading) {
      params.set("slope", "1");
    }
    if (this.state.include_calibration_page) {
      params.set("cal", "1");
    }
    const nextUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState(null, "", nextUrl);
  }

  private applyPlaceResult(place: GeocodeResult): void {
    this.clearStripPlanInternal(false);
    this.state.lat = place.lat;
    this.state.lon = place.lon;
    this.state.center = formatCoords(place.lat, place.lon);
    this.sidebar.setCenterInput(this.state.center, true);
    this.sidebar.setTitlePlaceholder(place.short_name.toUpperCase());
    this.syncUrlFromState();
    this.map.setCenter(place.lat, place.lon, { fly: true, zoom: 12 });
    this.requestFootprintImmediate({
      flyToResult: false,
      syncMarker: false,
    });
    this.showToast(`Centered on ${place.short_name}`, "info");
  }

  private applyPreset(preset: LocationPreset): void {
    this.clearStripPlanInternal(false);
    this.state.lat = preset.lat;
    this.state.lon = preset.lon;
    this.state.center = formatCoords(preset.lat, preset.lon);
    this.state.scale = preset.scale;
    this.state.paper = preset.paper;
    this.state.units = preset.units;
    if (preset.dem_source) {
      this.state.dem_source = preset.dem_source;
    }
    if (preset.au_overlays) {
      this.state.au_overlays = [...preset.au_overlays];
      this.state.us_overlays = [];
    }
    this.sidebar.setTitlePlaceholder(preset.defaultTitle);
    this.sidebar.syncControlsFromState();
    this.syncUrlFromState();
    this.map.setCenter(preset.lat, preset.lon, { fly: true, zoom: preset.zoom });
    this.requestFootprintImmediate({
      flyToResult: false,
      syncMarker: false,
    });
  }

  // ---------- Strip Atlas Planner ----------

  private async runStripPlan(options: {
    fitAfterLoad: boolean;
    silentToast: boolean;
  }): Promise<void> {
    const file = this.state.overlayFile;
    if (!file) return;

    this.stripAbort?.abort();
    const controller = new AbortController();
    this.stripAbort = controller;
    this.sidebar.setPlanningStrip(true);

    try {
      const plan = await fetchStripPlan(
        file,
        {
          scale: this.state.scale,
          paper: this.state.paper,
          orientation: this.state.orientation,
          units: this.state.units,
        },
        controller.signal
      );

      if (controller.signal.aborted) return;

      this.state.stripPlan = plan;
      this.state.include_elevation_profile = true;

      const anchor = plan.sheets[0];
      if (anchor) {
        this.state.lat = anchor.center_lat;
        this.state.lon = anchor.center_lon;
        this.state.center = formatCoords(anchor.center_lat, anchor.center_lon);
        this.map.setCenter(anchor.center_lat, anchor.center_lon, { fly: false });
      }

      this.map.updateFootprint(
        plan.footprint_geojson,
        plan.grid_geojson,
        this.state.carto.show_grid
      );
      this.map.updateStripRoute(plan.route_geojson);
      this.telemetry.updateStripPlan(plan);
      this.sidebar.syncControlsFromState(true);
      this.setFootprintError(null);

      if (options.fitAfterLoad) {
        this.fitSheetOnMap();
      }

      if (!options.silentToast) {
        const count = plan.sheet_count ?? plan.sheets.length;
        this.showToast(
          `Strip Atlas: ${count} sheet${count === 1 ? "" : "s"} · ${plan.route_distance_km.toFixed(1)} km`,
          "success"
        );
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Failed to plan strip atlas";
      this.setFootprintError(msg);
      this.showToast(msg, "error");
    } finally {
      if (!controller.signal.aborted) {
        this.sidebar.setPlanningStrip(false);
      }
    }
  }

  private clearStripPlanInternal(restoreFootprint: boolean): void {
    this.stripAbort?.abort();
    this.stripAbort = null;
    if (!this.state.stripPlan) return;
    this.state.stripPlan = null;
    this.map.updateStripRoute(null);
    this.sidebar.syncControlsFromState();
    if (restoreFootprint) {
      this.requestFootprintImmediate({
        flyToResult: false,
        syncMarker: false,
      });
    }
  }

  // ---------- Waypoints & Tactical Route Metrics ----------

  private handleAddWaypoint(lat: number, lon: number): void {
    const num = this.state.waypoints.length + 1;
    const isFirst = num === 1;
    this.state.waypoints = [
      ...this.state.waypoints,
      {
        label: `WP${num}`,
        lat: Number(lat.toFixed(6)),
        lon: Number(lon.toFixed(6)),
        category: "waypoint",
      },
    ];
    if (isFirst) {
      this.state.include_route_card = true;
      this.sidebar.openWaypointsSection();
    }
    this.map.updateWaypoints(this.state.waypoints);
    this.sidebar.syncControlsFromState();
    this.scheduleRouteMetrics();
  }

  private handleWaypointEdit(
    index: number,
    patch: { label?: string; category?: WaypointCategory }
  ): void {
    const existing = this.state.waypoints[index];
    if (!existing) return;
    const updated = [...this.state.waypoints];
    updated[index] = {
      ...existing,
      label: patch.label !== undefined ? patch.label : existing.label,
      category:
        patch.category !== undefined ? patch.category : existing.category,
    };
    this.state.waypoints = updated;
    this.map.updateWaypoints(this.state.waypoints);
    this.scheduleRouteMetrics();
  }

  private handleWaypointDelete(index: number): void {
    this.state.waypoints = this.state.waypoints.filter((_, i) => i !== index);
    if (this.state.waypoints.length === 0) {
      this.state.include_route_card = false;
      this.sidebar.setRouteMetrics(null);
    }
    this.map.updateWaypoints(this.state.waypoints);
    this.sidebar.syncControlsFromState();
    if (this.state.waypoints.length > 0) {
      this.scheduleRouteMetrics();
    }
  }

  private handleWaypointsClear(): void {
    this.metricsAbort?.abort();
    this.state.waypoints = [];
    this.state.include_route_card = false;
    this.map.updateWaypoints([]);
    this.sidebar.setRouteMetrics(null);
    this.sidebar.syncControlsFromState();
  }

  private scheduleRouteMetrics(): void {
    if (this.metricsTimer !== null) {
      window.clearTimeout(this.metricsTimer);
    }
    this.metricsTimer = window.setTimeout(() => {
      this.metricsTimer = null;
      void this.loadRouteMetrics();
    }, ROUTE_METRICS_DEBOUNCE_MS);
  }

  private async loadRouteMetrics(): Promise<void> {
    if (this.state.waypoints.length === 0) {
      this.sidebar.setRouteMetrics(null);
      return;
    }

    this.metricsAbort?.abort();
    const controller = new AbortController();
    this.metricsAbort = controller;

    try {
      const metrics = await fetchRouteMetrics(
        this.state.waypoints,
        this.state.units,
        null,
        controller.signal
      );
      if (controller.signal.aborted) return;
      this.sidebar.setRouteMetrics(metrics);
    } catch {
      // Keep local waypoint display cleanly if backend is restarting
    }
  }

  // ---------- Footprint, Health, Packs & Jobs ----------

  private setDrawerOpen(open: boolean): void {
    this.drawerOpen = open;
    this.shellEl.classList.toggle("drawer-open", open);
    this.updateQueueBadge();
  }

  private scheduleFootprint(): void {
    if (this.footprintTimer !== null) {
      window.clearTimeout(this.footprintTimer);
    }
    this.footprintTimer = window.setTimeout(() => {
      this.footprintTimer = null;
      void this.loadFootprint({
        flyToResult: false,
        syncMarker: false,
      });
    }, FOOTPRINT_DEBOUNCE_MS);
  }

  private requestFootprintImmediate(options: {
    flyToResult: boolean;
    syncMarker: boolean;
    fitAfterLoad?: boolean;
  }): void {
    if (this.footprintTimer !== null) {
      window.clearTimeout(this.footprintTimer);
      this.footprintTimer = null;
    }
    void this.loadFootprint(options);
  }

  private async loadFootprint(options: {
    flyToResult: boolean;
    syncMarker: boolean;
    fitAfterLoad?: boolean;
  }): Promise<void> {
    this.footprintAbort?.abort();
    const controller = new AbortController();
    this.footprintAbort = controller;

    try {
      const fp = await fetchFootprint(
        {
          center: this.state.center,
          scale: this.state.scale,
          paper: this.state.paper,
          orientation: this.state.orientation,
          units: this.state.units,
          grid_cols: this.state.grid_cols,
          grid_rows: this.state.grid_rows,
        },
        controller.signal
      );

      if (controller.signal.aborted) return;

      this.latestFootprint = fp;
      this.state.lat = fp.center_lat;
      this.state.lon = fp.center_lon;
      this.telemetry.updateFootprint(fp);
      this.map.updateFootprint(
        fp.footprint_geojson,
        fp.grid_geojson,
        this.state.carto.show_grid
      );
      if (options.syncMarker && !this.map.isDragging()) {
        this.map.setCenter(fp.center_lat, fp.center_lon, {
          fly: options.flyToResult,
        });
      }
      if (options.fitAfterLoad) {
        this.fitSheetOnMap();
      }
      this.setFootprintError(null);
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Failed to compute footprint";
      this.setFootprintError(msg);
    }
  }

  private setFootprintError(msg: string | null): void {
    if (!msg) {
      this.footprintBannerEl.classList.add("hidden");
      this.footprintBannerEl.textContent = "";
    } else {
      this.footprintBannerEl.classList.remove("hidden");
      this.footprintBannerEl.textContent = `Footprint error: ${msg}`;
    }
  }

  private async loadHealth(): Promise<void> {
    try {
      const health = await fetchHealth();
      this.telemetry.updateHealth(health);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unavailable";
      this.telemetry.updateHealth(null, msg);
    }
  }

  private async loadPacks(notify: boolean): Promise<void> {
    try {
      const res = await fetchPacks();
      this.sidebar.setPacks(res.packs);
      if (notify) {
        this.showToast(
          res.packs.length > 0
            ? `Loaded ${res.packs.length} offline pack${res.packs.length === 1 ? "" : "s"}`
            : "No offline packs installed — using Cloud-Native On-Demand",
          "info"
        );
      }
    } catch {
      this.sidebar.setPacks([]);
    }
  }

  private scheduleNextJobsPoll(): void {
    if (this.jobsPollTimer !== null) {
      window.clearTimeout(this.jobsPollTimer);
    }
    const hasActive = this.jobs.some(
      (j) => j.status === "queued" || j.status === "running"
    );
    const delay = hasActive ? ACTIVE_JOBS_POLL_MS : IDLE_JOBS_POLL_MS;
    this.jobsPollTimer = window.setTimeout(() => {
      void this.pollJobsOnce();
    }, delay);
  }

  private async pollJobsOnce(): Promise<void> {
    try {
      const jobs = await fetchJobs();
      this.jobs = jobs;
      this.drawer.setJobs(jobs);
      this.updateQueueBadge();
    } catch {
      // Keep last known jobs silently while backend restarts or offline
    } finally {
      this.scheduleNextJobsPoll();
    }
  }

  private updateQueueBadge(): void {
    const activeCount = this.jobs.filter(
      (j) => j.status === "queued" || j.status === "running"
    ).length;
    this.telemetry.updateQueueCounts(
      activeCount,
      this.jobs.length,
      this.drawerOpen
    );
  }

  private buildJobPayload(): CreateJobPayload {
    const payload: CreateJobPayload = {
      center:
        this.state.center.trim() || formatCoords(this.state.lat, this.state.lon),
      scale: this.state.scale,
      paper: this.state.paper,
      orientation: this.state.orientation,
      color_mode: this.state.color_mode,
      units: this.state.units,
      title: this.state.title.trim() || null,
      dem_source: this.state.dem_source,
      pack_id: this.state.pack_id || null,
      au_overlays: [...this.state.au_overlays],
      us_overlays: [...this.state.us_overlays],
      carto: { ...this.state.carto },
      outputs: { ...this.state.outputs },
      grid_cols: this.state.stripPlan ? 1 : this.state.grid_cols,
      grid_rows: this.state.stripPlan ? 1 : this.state.grid_rows,
      waypoints: this.state.waypoints.map((wp) => ({ ...wp })),
      include_calibration_page:
        this.state.outputs.pdf && this.state.include_calibration_page,
      include_route_card:
        this.state.outputs.pdf && this.state.include_route_card,
      include_elevation_profile:
        this.state.outputs.pdf && this.state.include_elevation_profile,
    };

    if (this.state.stripPlan && this.state.stripPlan.sheets.length > 0) {
      payload.strip_sheets = this.state.stripPlan.sheets.map((s) => ({ ...s }));
    }

    return payload;
  }

  private async handleSubmitJob(): Promise<void> {
    this.sidebar.setSubmitting(true);
    this.sidebar.setFormError(null);

    try {
      const payload = this.buildJobPayload();
      const job = await submitJob(payload, this.state.overlayFile);
      this.jobs = [job, ...this.jobs.filter((j) => j.job_id !== job.job_id)];
      this.drawer.setJobs(this.jobs);
      this.setDrawerOpen(true);
      this.showToast(
        `Queued sheet job #${job.job_id.slice(0, 8)}`,
        "success"
      );
      void this.loadHealth();
      this.scheduleNextJobsPoll();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to submit render job";
      this.sidebar.setFormError(msg);
      this.showToast(msg, "error");
    } finally {
      this.sidebar.setSubmitting(false);
    }
  }

  private async handleCancelJob(jobId: string): Promise<void> {
    try {
      const updated = await cancelJob(jobId);
      this.jobs = this.jobs.map((j) => (j.job_id === jobId ? updated : j));
      this.drawer.setJobs(this.jobs);
      this.updateQueueBadge();
      this.showToast(`Cancelled job #${jobId.slice(0, 8)}`, "info");
    } catch (err) {
      this.drawer.clearBusy(jobId);
      const msg = err instanceof Error ? err.message : "Failed to cancel job";
      this.showToast(msg, "error");
    }
  }

  private async handleDeleteJob(jobId: string): Promise<void> {
    try {
      await deleteJob(jobId);
      this.jobs = this.jobs.filter((j) => j.job_id !== jobId);
      this.drawer.setJobs(this.jobs);
      this.updateQueueBadge();
      this.showToast(`Deleted job #${jobId.slice(0, 8)}`, "info");
    } catch (err) {
      this.drawer.clearBusy(jobId);
      const msg = err instanceof Error ? err.message : "Failed to delete job";
      this.showToast(msg, "error");
    }
  }

  private copyText(text: string, label: string): void {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard
        .writeText(text)
        .then(() => this.showToast(label, "success"))
        .catch(() => this.showToast("Clipboard unavailable", "error"));
    } else {
      this.showToast("Clipboard unavailable", "error");
    }
  }

  private showToast(
    message: string,
    kind: "success" | "error" | "info"
  ): void {
    const el = document.createElement("div");
    el.className = `gf-toast ${kind}`;
    el.textContent = message;
    this.toastContainerEl.appendChild(el);
    window.setTimeout(() => {
      el.remove();
    }, 3600);
  }
}

const mount = document.getElementById("app");
if (!mount) {
  throw new Error("Missing #app root container");
}
new GridForgeApp(mount);
