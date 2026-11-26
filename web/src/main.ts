import "./styles.css";
import {
  cancelJob,
  deleteJob,
  fetchBasemapInfo,
  fetchFootprint,
  fetchHealth,
  fetchJobs,
  fetchPacks,
  fetchRFLinkProfile,
  fetchRouteMetrics,
  fetchRulerMetrics,
  fetchStripPlan,
  fetchViewshedPreview,
  submitJob,
} from "./api";
import { JobsDrawer } from "./components/jobsDrawer";
import { PacksModal } from "./components/packsModal";
import { ProjectsDrawer } from "./components/projectsDrawer";
import { SidebarPanel } from "./components/sidebar";
import { TelemetryBar } from "./components/telemetry";
import { GridForgeMap } from "./map";
import {
  ALL_AU_OVERLAY_KINDS,
  BASEMAP_OPTIONS,
  CA_OVERLAY_OPTIONS,
  COLOR_MODE_OPTIONS,
  DEM_SOURCE_OPTIONS,
  NZ_OVERLAY_OPTIONS,
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
  CAOverlayKind,
  ColorMode,
  CreateJobPayload,
  DemSourceKind,
  FootprintResponse,
  GeocodeResult,
  JobRecord,
  LocationPreset,
  MapInteractionMode,
  NZOverlayKind,
  Orientation,
  PaperSize,
  RFLinkEntry,
  RFLinkSpec,
  RulerMetricsResponse,
  SavedProject,
  TacticalGraphic,
  USOverlayKind,
  Units,
  ViewshedEntry,
  ViewshedObserver,
  WaypointCategory,
  WaypointInput,
} from "./types";

const FOOTPRINT_DEBOUNCE_MS = 180;
const ROUTE_METRICS_DEBOUNCE_MS = 150;
const ACTIVE_JOBS_POLL_MS = 2000;
const IDLE_JOBS_POLL_MS = 10000;
const HEALTH_POLL_MS = 5000;

const VALID_SCALES = new Set(SCALE_OPTIONS.map((s) => s.value));
const VALID_PAPERS = new Set<string>(PAPER_OPTIONS.map((p) => p.value));
const VALID_MODES = new Set<string>(COLOR_MODE_OPTIONS.map((m) => m.value));
const VALID_DEMS = new Set<string>(DEM_SOURCE_OPTIONS.map((d) => d.value));
const VALID_AU_OV = new Set<string>(ALL_AU_OVERLAY_KINDS);
const VALID_NZ_OV = new Set<string>(NZ_OVERLAY_OPTIONS.map((o) => o.value));
const VALID_CA_OV = new Set<string>(CA_OVERLAY_OPTIONS.map((o) => o.value));
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
  if (modeParam && VALID_MODES.has(modeParam)) {
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

  const packParam = params.get("pack")?.trim();
  if (packParam && /^[a-zA-Z0-9_-]{1,64}$/.test(packParam)) {
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

  if (params.get("slope") === "1" || params.get("slope") === "true") {
    state.carto.show_slope_shading = true;
  }
  if (params.get("cal") === "1") {
    state.include_calibration_page = true;
  }
  if (params.get("fold") === "1") {
    state.show_fold_marks = true;
  }
  if (params.get("index") === "1") {
    state.include_atlas_index = true;
  } else if (params.get("index") === "0") {
    state.include_atlas_index = false;
  }

  const losParam = params.get("los")?.trim();
  if (losParam) {
    const parts = losParam.split(";").slice(0, 4);
    parts.forEach((raw, idx) => {
      const segs = raw.split(",");
      const lat = Number(segs[0]);
      const lon = Number(segs[1]);
      if (
        !Number.isFinite(lat) ||
        !Number.isFinite(lon) ||
        lat < -80 ||
        lat > 84 ||
        lon < -180 ||
        lon > 180
      ) {
        return;
      }
      const obsH = Number.isFinite(Number(segs[2]))
        ? Math.min(500, Math.max(0, Number(segs[2])))
        : 2;
      const tgtH = Number.isFinite(Number(segs[3]))
        ? Math.min(500, Math.max(0, Number(segs[3])))
        : 1.5;
      const maxKm = Number.isFinite(Number(segs[4]))
        ? Math.min(25, Math.max(0.25, Number(segs[4])))
        : 10;
      state.viewsheds.push({
        id: `vs-init-${idx + 1}`,
        observer: {
          label: `OBS${idx + 1}`,
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

  return { hasCustomCenter, needsFlyToResolvedCenter };
}

class GridForgeApp {
  private readonly state = createInitialFormState();
  private readonly shellEl: HTMLElement;
  private readonly telemetry: TelemetryBar;
  private readonly sidebar: SidebarPanel;
  private readonly drawer: JobsDrawer;
  private readonly projectsDrawer: ProjectsDrawer;
  private readonly packsModal: PacksModal;
  private readonly map: GridForgeMap;
  private readonly basemapSwitcherEl: HTMLElement;
  private readonly offlineReadyBadgeEl: HTMLButtonElement;
  private readonly footprintBannerEl: HTMLElement;
  private readonly mapHintEl: HTMLElement;
  private readonly mapHintTextEl: HTMLElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly collapseBtnEl: HTMLButtonElement;
  private readonly toolBarButtons: NodeListOf<HTMLButtonElement>;

  private sidebarCollapsed = false;
  private drawerOpen = false;
  private projectsOpen = false;
  private interactionMode: MapInteractionMode = "pan";
  private activeBasemap: BasemapId = "opentopo";
  private latestFootprint: FootprintResponse | null = null;
  private jobs: JobRecord[] = [];
  private readonly initialFlyToResolved: boolean;

  private rulerPoints: { lat: number; lon: number }[] = [];
  private rulerMetrics: RulerMetricsResponse | null = null;
  private defaultRingRadiiKm: number[] = [1, 3, 5];
  private sectorDraft: [number, number][] = [];
  private rfPendingFrom: { lat: number; lon: number; label: string } | null =
    null;

  private footprintTimer: number | null = null;
  private footprintAbort: AbortController | null = null;
  private stripAbort: AbortController | null = null;
  private metricsTimer: number | null = null;
  private metricsAbort: AbortController | null = null;
  private rulerAbort: AbortController | null = null;
  private viewshedAborts = new Map<string, AbortController>();
  private rfLinkAborts = new Map<string, AbortController>();
  private jobsPollTimer: number | null = null;
  private viewshedSeq = 1;
  private rfLinkSeq = 1;

  constructor(root: HTMLElement) {
    const urlHydration = hydrateStateFromUrl(this.state);
    this.initialFlyToResolved = urlHydration.needsFlyToResolvedCenter;
    this.viewshedSeq = this.state.viewsheds.length + 1;

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
            <div class="gf-toolbar-row">
              <button
                type="button"
                class="gf-offline-ready-pill hidden"
                data-role="offline-ready-pill"
                title="External tiles failed — click to switch to local Offline MBTiles basemap"
              >Offline Basemap Ready</button>
              <div class="gf-basemap-switcher" data-role="basemap-switcher" role="group" aria-label="Basemap switcher">
                ${BASEMAP_OPTIONS.map(
                  (b) =>
                    `<button type="button" class="gf-basemap-btn ${b.id === this.activeBasemap ? "active" : ""}" data-basemap="${escapeHtml(b.id)}">${escapeHtml(b.label)}</button>`
                ).join("")}
              </div>
              <div class="gf-map-actions">
                <button type="button" class="gf-map-icon-btn" data-map-action="fit" title="Fit sheet footprint (F)">⌖ Fit</button>
                <button type="button" class="gf-map-icon-btn" data-map-action="zoom-in" title="Zoom in">+</button>
                <button type="button" class="gf-map-icon-btn" data-map-action="zoom-out" title="Zoom out">−</button>
                <button type="button" class="gf-map-icon-btn" data-map-action="north" title="Reset north">N</button>
              </div>
            </div>
            <div class="gf-toolbar-row">
              <div class="gf-map-actions gf-tactical-bar" role="group" aria-label="Tactical map tools">
                <button type="button" class="gf-map-icon-btn gf-markup-btn" data-map-tool="markup" aria-pressed="false" title="Drop waypoints on map (M)">⌖ WP</button>
                <button type="button" class="gf-map-icon-btn gf-ruler-btn" data-map-tool="ruler" aria-pressed="false" title="Measure distance, magnetic bearing & grade (R)">📏 Ruler</button>
                <button type="button" class="gf-map-icon-btn gf-ring-btn" data-map-tool="ring" aria-pressed="false" title="Drop concentric range rings on map">◎ Ring</button>
                <button type="button" class="gf-map-icon-btn gf-sector-btn" data-map-tool="sector" aria-pressed="false" title="Draw shaded AOI sector polygon">⬠ Sector</button>
                <button type="button" class="gf-map-icon-btn gf-los-btn" data-map-tool="los" aria-pressed="false" title="Drop RF / LOS viewshed observer (V)">📡 LOS</button>
              </div>
            </div>
          </div>

          <div class="gf-footprint-banner hidden" data-role="footprint-banner"></div>

          <div class="gf-map-hint" data-role="map-hint">
            <span data-role="map-hint-text">Click map or drag reticle · F to fit</span>
            <span class="mono" data-role="cursor-coords">—</span>
          </div>

          <aside data-role="drawer"></aside>
          <aside data-role="projects-drawer"></aside>
        </main>
        <div data-role="packs-modal"></div>
        <div class="gf-toast-container" data-role="toasts"></div>
      </div>
    `;

    this.shellEl = this.q(root, "[data-role='shell']");
    this.basemapSwitcherEl = this.q(root, "[data-role='basemap-switcher']");
    this.offlineReadyBadgeEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='offline-ready-pill']"
    );
    this.footprintBannerEl = this.q(root, "[data-role='footprint-banner']");
    this.mapHintEl = this.q(root, "[data-role='map-hint']");
    this.mapHintTextEl = this.q(root, "[data-role='map-hint-text']");
    this.cursorCoordsEl = this.q(root, "[data-role='cursor-coords']");
    this.toastContainerEl = this.q(root, "[data-role='toasts']");
    this.collapseBtnEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='sidebar-toggle']"
    );
    this.toolBarButtons = root.querySelectorAll<HTMLButtonElement>(
      "[data-map-tool]"
    );

    this.telemetry = new TelemetryBar(this.q(root, "[data-role='topbar']"), {
      onToggleDrawer: () => this.setDrawerOpen(!this.drawerOpen),
      onToggleProjects: () => this.setProjectsOpen(!this.projectsOpen),
      onCopyMgrs: (mgrs) => this.copyText(mgrs, `Copied ${mgrs}`),
    });

    this.packsModal = new PacksModal(this.q(root, "[data-role='packs-modal']"), {
      onPacksUpdated: (packs) => {
        this.sidebar.setPacks(packs);
        void this.loadHealth();
      },
      onSelectPack: (packId) => {
        this.state.pack_id = packId;
        this.sidebar.syncControlsFromState();
        this.syncUrlFromState();
        void this.loadBasemapInfo(packId);
        this.showToast(`Selected offline pack "${packId}"`, "success");
      },
      onBasemapRefresh: () => {
        void this.loadBasemapInfo(this.state.pack_id || null);
      },
      onToast: (msg, kind) => this.showToast(msg, kind),
      getCurrentBBoxString: () => this.map.getFootprintBBoxString(),
    });

    this.projectsDrawer = new ProjectsDrawer(
      this.q(root, "[data-role='projects-drawer']"),
      {
        onClose: () => this.setProjectsOpen(false),
        onLoadProject: (proj) => this.loadProject(proj),
        getCurrentSnapshot: () => this.buildProjectSnapshot(),
        onToast: (msg, kind) => this.showToast(msg, kind),
      }
    );

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
        onPackChange: (packId) => {
          void this.loadBasemapInfo(packId || null);
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
        onToggleInteractionMode: (mode) =>
          this.setInteractionMode(
            this.interactionMode === mode ? "pan" : mode
          ),
        onWaypointEdit: (idx, patch) => this.handleWaypointEdit(idx, patch),
        onWaypointDelete: (idx) => this.handleWaypointDelete(idx),
        onWaypointsClear: () => this.handleWaypointsClear(),
        onRulerPinToPdf: () => this.handleRulerPinToPdf(),
        onRulerClear: () => this.handleRulerClear(),
        onRingRadiiChange: (radii) => {
          this.defaultRingRadiiKm = radii;
        },
        onSectorFinish: () => this.handleSectorFinish(),
        onSectorCancel: () => this.handleSectorCancel(),
        onTacticalLabelEdit: (idx, label) =>
          this.handleTacticalLabelEdit(idx, label),
        onTacticalDelete: (idx) => this.handleTacticalDelete(idx),
        onTacticalClear: () => this.handleTacticalClear(),
        onViewshedEdit: (id, patch, recompute) =>
          this.handleViewshedEdit(id, patch, recompute),
        onViewshedRecompute: (id) => void this.computeViewshedEntry(id),
        onViewshedDelete: (id) => this.handleViewshedDelete(id),
        onViewshedsClear: () => this.handleViewshedsClear(),
        onRFLinkUseObservers: () => this.handleRFLinkUseObservers(),
        onRFLinkRecompute: (id) => void this.computeRFLinkEntry(id),
        onRFLinkDelete: (id) => this.handleRFLinkDelete(id),
        onRFLinksClear: () => this.handleRFLinksClear(),
        onManagePacks: () => this.packsModal.open(),
        onRefreshPacks: () => void this.loadPacks(true),
        onSubmitJob: () => void this.handleSubmitJob(),
        onCopyCli: () => {
          const result = buildCliCommand(this.state);
          if ("error" in result) {
            this.sidebar.setFormError(result.error);
            return;
          }
          this.sidebar.setFormError(null);
          this.copyText(
            result.command,
            this.state.overlayFile
              ? "Command copied. Place the selected file in ./out/inputs/ before running."
              : "Command copied. Run from the project directory. Results appear in ./out/."
          );
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
        onMapViewshedClick: (lat, lon) => this.handleAddViewshed(lat, lon),
        onMapRFLinkClick: (lat, lon) => this.handleRFLinkMapClick(lat, lon),
        onMapRulerClick: (lat, lon) => this.handleRulerClick(lat, lon),
        onMapRingClick: (lat, lon) => this.handleDropRangeRing(lat, lon),
        onMapSectorClick: (lat, lon) => this.handleSectorClick(lat, lon),
        onMapSectorFinish: () => this.handleSectorFinish(),
        onCursorMove: (lat, lon) => {
          this.cursorCoordsEl.textContent = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
        },
        onExternalTileFailure: () => {
          this.offlineReadyBadgeEl.classList.remove("hidden");
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

    this.basemapSwitcherEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("[data-basemap]");
      if (!btn) return;
      const id = btn.dataset.basemap as BasemapId;
      this.selectBasemap(id);
    });

    this.offlineReadyBadgeEl.addEventListener("click", () => {
      this.selectBasemap("offline");
      this.offlineReadyBadgeEl.classList.add("hidden");
      this.showToast("Switched to local Offline MBTiles basemap", "info");
    });

    root
      .querySelectorAll<HTMLButtonElement>("[data-map-action]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const action = btn.dataset.mapAction;
          if (action === "fit") {
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

    this.toolBarButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const tool = btn.dataset.mapTool as MapInteractionMode;
        if (!tool) return;
        this.setInteractionMode(this.interactionMode === tool ? "pan" : tool);
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

      if (e.key === "Escape") {
        if (this.packsModal.opened) {
          this.packsModal.close();
        } else if (this.interactionMode !== "pan") {
          this.setInteractionMode("pan");
        } else if (this.projectsOpen) {
          this.setProjectsOpen(false);
        } else if (this.drawerOpen) {
          this.setDrawerOpen(false);
        }
        return;
      }

      if (this.packsModal.opened || inInput) return;

      if (e.key === "Enter" && this.interactionMode === "sector") {
        if (this.sectorDraft.length >= 3) {
          e.preventDefault();
          this.handleSectorFinish();
        }
        return;
      }

      if (e.key === "/") {
        e.preventDefault();
        if (this.sidebarCollapsed) {
          this.collapseBtnEl.click();
        }
        this.sidebar.focusCenterInput();
      } else if (
        (e.key === "f" || e.key === "F") &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        this.fitSheetOnMap();
      } else if (
        (e.key === "m" || e.key === "M") &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        this.setInteractionMode(
          this.interactionMode === "markup" ? "pan" : "markup"
        );
      } else if (
        (e.key === "r" || e.key === "R") &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        this.setInteractionMode(
          this.interactionMode === "ruler" ? "pan" : "ruler"
        );
      } else if (
        (e.key === "v" || e.key === "V") &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        this.setInteractionMode(this.interactionMode === "los" ? "pan" : "los");
      }
    });
  }

  private selectBasemap(id: BasemapId): void {
    this.activeBasemap = id;
    this.map.setBasemap(id);
    if (id === "offline") {
      this.offlineReadyBadgeEl.classList.add("hidden");
    }
    this.basemapSwitcherEl
      .querySelectorAll<HTMLButtonElement>("[data-basemap]")
      .forEach((b) => b.classList.toggle("active", b.dataset.basemap === id));
  }

  private updateMapHintForMode(): void {
    const m = this.interactionMode;
    this.mapHintEl.classList.toggle("markup-active", m === "markup");
    this.mapHintEl.classList.toggle("los-active", m === "los");
    this.mapHintEl.classList.toggle(
      "tool-cyan-active",
      m === "ruler" || m === "ring" || m === "rf-link"
    );
    this.mapHintEl.classList.toggle("sector-active", m === "sector");

    if (m === "los") {
      this.mapHintTextEl.textContent =
        "LOS ON: click map to place observer · V / Esc to exit";
    } else if (m === "markup") {
      this.mapHintTextEl.textContent =
        "Markup ON: click map to drop WP · M / Esc to exit";
    } else if (m === "ruler") {
      this.mapHintTextEl.textContent =
        "Ruler ON: click points to measure · R / Esc to exit";
    } else if (m === "ring") {
      this.mapHintTextEl.textContent =
        "Range Ring ON: click map to drop concentric rings · Esc to exit";
    } else if (m === "sector") {
      this.mapHintTextEl.textContent =
        "Sector AOI ON: click ≥3 vertices · dbl-click / Enter to finish";
    } else if (m === "rf-link") {
      this.mapHintTextEl.textContent = this.rfPendingFrom
        ? "RF Link ON: click Point B on map · Esc to cancel"
        : "RF Link ON: click Point A on map · Esc to cancel";
    } else {
      this.mapHintTextEl.textContent = "Click map or drag reticle · F to fit";
    }
  }

  private setInteractionMode(mode: MapInteractionMode): void {
    if (this.interactionMode === "rf-link" && mode !== "rf-link") {
      this.rfPendingFrom = null;
      this.map.updateRFLinks(this.state.rfLinks, null);
    }
    if (this.interactionMode === "sector" && mode !== "sector") {
      this.sectorDraft = [];
      this.sidebar.setSectorDraftCount(0);
      this.map.updateTacticalGraphics(this.state.tacticalGraphics, []);
    }

    this.interactionMode = mode;
    this.map.setInteractionMode(mode);
    this.sidebar.setInteractionMode(mode, this.rfPendingFrom !== null);

    this.toolBarButtons.forEach((btn) => {
      const active = btn.dataset.mapTool === mode;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    });

    this.updateMapHintForMode();

    if (
      mode === "markup" ||
      mode === "ruler" ||
      mode === "ring" ||
      mode === "sector"
    ) {
      this.sidebar.openWaypointsSection();
    } else if (mode === "los" || mode === "rf-link") {
      this.sidebar.openViewshedSection();
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
    void this.loadBasemapInfo(this.state.pack_id || null);
    void this.loadHealth();
    void this.loadPacks(false);
    void this.pollJobsOnce();

    if (this.state.viewsheds.length > 0) {
      this.sidebar.openViewshedSection();
      this.map.updateViewsheds(this.state.viewsheds);
      for (const vs of this.state.viewsheds) {
        void this.computeViewshedEntry(vs.id);
      }
    }

    window.setInterval(() => void this.loadHealth(), HEALTH_POLL_MS);
    window.setInterval(() => this.drawer.tickElapsed(), 1000);
  }

  private async loadBasemapInfo(packId?: string | null): Promise<void> {
    try {
      const info = await fetchBasemapInfo(packId);
      const existingBtn = this.basemapSwitcherEl.querySelector<HTMLButtonElement>(
        "[data-basemap='offline']"
      );

      if (!info.available || !info.format) {
        this.map.enableOfflineBasemap(info);
        if (existingBtn) existingBtn.remove();
        if (this.activeBasemap === "offline") {
          this.selectBasemap("opentopo");
        }
        return;
      }

      const enriched = packId && !info.pack_id ? { ...info, pack_id: packId } : info;
      this.map.enableOfflineBasemap(enriched);
      const titleText = info.name
        ? `Local Offline MBTiles (${info.name})`
        : "Local Offline MBTiles Basemap";

      if (!existingBtn) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "gf-basemap-btn";
        btn.dataset.basemap = "offline";
        btn.textContent = "Offline";
        btn.title = titleText;
        this.basemapSwitcherEl.appendChild(btn);
      } else {
        existingBtn.title = titleText;
      }
    } catch {
      // Offline basemap endpoint optional / unavailable
    }
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
    if (this.state.pack_id) {
      params.set("pack", this.state.pack_id);
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
    if (this.state.nz_overlays.length > 0) {
      params.set("nz", this.state.nz_overlays.join(","));
    }
    if (this.state.ca_overlays.length > 0) {
      params.set("ca", this.state.ca_overlays.join(","));
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
    if (this.state.show_fold_marks) {
      params.set("fold", "1");
    }
    if (this.state.include_atlas_index) {
      params.set("index", "1");
    }
    if (this.state.viewsheds.length > 0) {
      const encoded = this.state.viewsheds
        .map((v) => {
          const o = v.observer;
          return `${o.lat.toFixed(5)},${o.lon.toFixed(5)},${o.observer_height_m},${o.target_height_m},${Number((o.max_distance_m / 1000).toFixed(2))}`;
        })
        .join(";");
      params.set("los", encoded);
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
    this.state.dem_source = preset.dem_source ?? "auto";
    this.state.au_overlays = preset.au_overlays ? [...preset.au_overlays] : [];
    this.state.nz_overlays = preset.nz_overlays ? [...preset.nz_overlays] : [];
    this.state.ca_overlays = preset.ca_overlays ? [...preset.ca_overlays] : [];
    this.state.us_overlays = preset.us_overlays ? [...preset.us_overlays] : [];
    this.sidebar.setTitlePlaceholder(preset.defaultTitle);
    this.sidebar.syncControlsFromState();
    this.syncUrlFromState();
    this.map.setCenter(preset.lat, preset.lon, { fly: true, zoom: preset.zoom });
    this.requestFootprintImmediate({
      flyToResult: false,
      syncMarker: false,
    });
  }

  // ---------- Saved Trip Projects ----------

  private buildProjectSnapshot(): {
    suggestedName: string;
    state: Record<string, unknown>;
    last_job_id: string | null;
  } {
    const latestOkJob = this.jobs.find((j) => j.status === "succeeded") ?? null;
    const suggestedName =
      this.state.title.trim() ||
      this.latestFootprint?.center_mgrs_10m ||
      this.state.center;

    return {
      suggestedName,
      last_job_id: latestOkJob?.job_id ?? null,
      state: {
        center: this.state.center,
        lat: this.state.lat,
        lon: this.state.lon,
        title: this.state.title,
        scale: this.state.scale,
        paper: this.state.paper,
        orientation: this.state.orientation,
        color_mode: this.state.color_mode,
        units: this.state.units,
        grid_cols: this.state.grid_cols,
        grid_rows: this.state.grid_rows,
        dem_source: this.state.dem_source,
        pack_id: this.state.pack_id,
        au_overlays: [...this.state.au_overlays],
        nz_overlays: [...this.state.nz_overlays],
        ca_overlays: [...this.state.ca_overlays],
        us_overlays: [...this.state.us_overlays],
        carto: { ...this.state.carto },
        outputs: { ...this.state.outputs },
        waypoints: this.state.waypoints.map((w) => ({ ...w })),
        viewsheds: this.state.viewsheds.map((v) => ({ ...v.observer })),
        rf_links: this.state.rfLinks.map((l) => ({ ...l.spec })),
        tactical_graphics: this.state.tacticalGraphics.map((g) => ({
          ...g,
          points: g.points.map((p) => [p[0], p[1]]),
          radii_km: [...g.radii_km],
        })),
        include_calibration_page: this.state.include_calibration_page,
        include_route_card: this.state.include_route_card,
        include_elevation_profile: this.state.include_elevation_profile,
        include_atlas_index: this.state.include_atlas_index,
        include_rf_link_card: this.state.include_rf_link_card,
        show_fold_marks: this.state.show_fold_marks,
      },
    };
  }

  private loadProject(project: SavedProject): void {
    const s = project.state as Record<string, unknown>;
    const defaults = createInitialFormState();

    this.clearStripPlanInternal(false);
    this.handleRulerClear();
    this.setInteractionMode("pan");

    if (typeof s.center === "string" && s.center.trim()) {
      this.state.center = s.center.trim();
    }
    if (typeof s.lat === "number" && Number.isFinite(s.lat)) {
      this.state.lat = s.lat;
    }
    if (typeof s.lon === "number" && Number.isFinite(s.lon)) {
      this.state.lon = s.lon;
    }
    this.state.title = typeof s.title === "string" ? s.title : "";
    if (typeof s.scale === "number" && VALID_SCALES.has(s.scale)) {
      this.state.scale = s.scale;
    }
    if (typeof s.paper === "string" && VALID_PAPERS.has(s.paper)) {
      this.state.paper = s.paper as PaperSize;
    }
    if (s.orientation === "portrait" || s.orientation === "landscape") {
      this.state.orientation = s.orientation;
    }
    if (typeof s.color_mode === "string" && VALID_MODES.has(s.color_mode)) {
      this.state.color_mode = s.color_mode as ColorMode;
    }
    if (s.units === "metric" || s.units === "imperial") {
      this.state.units = s.units;
    }
    if (typeof s.grid_cols === "number" && s.grid_cols >= 1 && s.grid_cols <= 3) {
      this.state.grid_cols = s.grid_cols;
    }
    if (typeof s.grid_rows === "number" && s.grid_rows >= 1 && s.grid_rows <= 3) {
      this.state.grid_rows = s.grid_rows;
    }
    if (typeof s.dem_source === "string" && VALID_DEMS.has(s.dem_source)) {
      this.state.dem_source = s.dem_source as DemSourceKind;
    }
    this.state.pack_id = typeof s.pack_id === "string" ? s.pack_id : "";

    this.state.au_overlays = Array.isArray(s.au_overlays)
      ? (s.au_overlays.filter(
          (x): x is AUOverlayKind => typeof x === "string" && VALID_AU_OV.has(x)
        ))
      : [];
    this.state.nz_overlays = Array.isArray(s.nz_overlays)
      ? (s.nz_overlays.filter(
          (x): x is NZOverlayKind => typeof x === "string" && VALID_NZ_OV.has(x)
        ))
      : [];
    this.state.ca_overlays = Array.isArray(s.ca_overlays)
      ? (s.ca_overlays.filter(
          (x): x is CAOverlayKind => typeof x === "string" && VALID_CA_OV.has(x)
        ))
      : [];
    this.state.us_overlays = Array.isArray(s.us_overlays)
      ? (s.us_overlays.filter(
          (x): x is USOverlayKind => typeof x === "string" && VALID_US_OV.has(x)
        ))
      : [];

    if (s.carto && typeof s.carto === "object") {
      this.state.carto = {
        ...defaults.carto,
        ...(s.carto as Partial<AppFormState["carto"]>),
      };
    }
    if (s.outputs && typeof s.outputs === "object") {
      this.state.outputs = {
        ...defaults.outputs,
        ...(s.outputs as Partial<AppFormState["outputs"]>),
      };
    }

    this.state.waypoints = Array.isArray(s.waypoints)
      ? (s.waypoints as WaypointInput[]).map((w) => ({ ...w }))
      : [];

    for (const ctrl of this.viewshedAborts.values()) ctrl.abort();
    this.viewshedAborts.clear();
    this.state.viewsheds = Array.isArray(s.viewsheds)
      ? (s.viewsheds as ViewshedObserver[]).map((obs) => ({
          id: `vs-${this.viewshedSeq++}`,
          observer: { ...obs },
          loading: false,
          error: null,
          preview: null,
        }))
      : [];

    for (const ctrl of this.rfLinkAborts.values()) ctrl.abort();
    this.rfLinkAborts.clear();
    this.state.rfLinks = Array.isArray(s.rf_links)
      ? (s.rf_links as RFLinkSpec[]).map((spec) => ({
          id: `rf-${this.rfLinkSeq++}`,
          spec: { ...spec },
          loading: false,
          error: null,
          profile: null,
        }))
      : [];

    this.state.tacticalGraphics = Array.isArray(s.tactical_graphics)
      ? (s.tactical_graphics as TacticalGraphic[]).map((g) => ({ ...g }))
      : [];

    this.state.include_calibration_page = Boolean(s.include_calibration_page);
    this.state.include_route_card = Boolean(s.include_route_card);
    this.state.include_elevation_profile = Boolean(s.include_elevation_profile);
    this.state.include_atlas_index = Boolean(s.include_atlas_index);
    this.state.include_rf_link_card = Boolean(s.include_rf_link_card);
    this.state.show_fold_marks = Boolean(s.show_fold_marks);

    this.sidebar.setFormError(null);
    this.sidebar.syncControlsFromState(true);
    this.syncUrlFromState();
    this.map.setCenter(this.state.lat, this.state.lon, { fly: true });
    this.map.updateWaypoints(this.state.waypoints);
    this.map.updateViewsheds(this.state.viewsheds);
    this.map.updateRFLinks(this.state.rfLinks, null);
    this.map.updateTacticalGraphics(this.state.tacticalGraphics, []);
    this.requestFootprintImmediate({
      flyToResult: false,
      syncMarker: true,
      fitAfterLoad: this.state.grid_cols * this.state.grid_rows > 1,
    });
    void this.loadBasemapInfo(this.state.pack_id || null);

    if (this.state.waypoints.length > 0) {
      this.scheduleRouteMetrics();
    } else {
      this.sidebar.setRouteMetrics(null);
    }
    for (const vs of this.state.viewsheds) {
      void this.computeViewshedEntry(vs.id);
    }
    for (const rf of this.state.rfLinks) {
      void this.computeRFLinkEntry(rf.id);
    }

    this.setProjectsOpen(false);
    this.showToast(`Loaded project "${project.name}"`, "success");
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
      this.state.include_atlas_index = true;

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
      this.syncUrlFromState();
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
    if (this.state.grid_cols * this.state.grid_rows === 1) {
      this.state.include_atlas_index = false;
    }
    this.map.updateStripRoute(null);
    this.sidebar.syncControlsFromState();
    this.syncUrlFromState();
    if (restoreFootprint) {
      this.requestFootprintImmediate({
        flyToResult: false,
        syncMarker: false,
      });
    }
  }

  // ---------- Tactical Ruler, Range Rings & Sectors ----------

  private handleRulerClick(lat: number, lon: number): void {
    this.rulerPoints = [
      ...this.rulerPoints,
      { lat: Number(lat.toFixed(6)), lon: Number(lon.toFixed(6)) },
    ];
    this.map.updateRuler(this.rulerPoints);
    if (this.rulerPoints.length < 2) {
      this.rulerMetrics = null;
      this.sidebar.setRulerState(this.rulerPoints, null, false);
      return;
    }
    void this.computeRulerMetrics();
  }

  private async computeRulerMetrics(): Promise<void> {
    this.rulerAbort?.abort();
    const controller = new AbortController();
    this.rulerAbort = controller;
    this.sidebar.setRulerState(this.rulerPoints, this.rulerMetrics, true);

    try {
      const res = await fetchRulerMetrics(
        this.rulerPoints,
        this.state.dem_source,
        this.state.pack_id || null,
        controller.signal
      );
      if (controller.signal.aborted) return;
      this.rulerMetrics = res;
      this.sidebar.setRulerState(this.rulerPoints, res, false);
    } catch (err) {
      if (controller.signal.aborted) return;
      this.sidebar.setRulerState(this.rulerPoints, this.rulerMetrics, false);
      const msg =
        err instanceof Error ? err.message : "Failed to compute ruler metrics";
      this.showToast(msg, "error");
    }
  }

  private handleRulerPinToPdf(): void {
    if (this.rulerPoints.length < 2) return;
    const distKm = this.rulerMetrics
      ? `${(this.rulerMetrics.total_distance_m / 1000).toFixed(2)} km`
      : "Line";
    const lastSeg = this.rulerMetrics?.segments.at(-1);
    const brgSuffix = lastSeg
      ? ` · ${Math.round(lastSeg.bearing_mag_deg).toString().padStart(3, "0")}°M`
      : "";
    const graphic: TacticalGraphic = {
      kind: "bearing_line",
      label: `${distKm}${brgSuffix}`,
      points: this.rulerPoints.map((p) => [p.lat, p.lon]),
      radii_km: [],
      color: "#F97316",
    };
    this.state.tacticalGraphics = [...this.state.tacticalGraphics, graphic];
    this.handleRulerClear();
    this.map.updateTacticalGraphics(
      this.state.tacticalGraphics,
      this.sectorDraft
    );
    this.sidebar.renderTacticalGraphicsList();
    this.showToast(`Pinned bearing line "${graphic.label}" to PDF`, "success");
  }

  private handleRulerClear(): void {
    this.rulerAbort?.abort();
    this.rulerAbort = null;
    this.rulerPoints = [];
    this.rulerMetrics = null;
    this.map.updateRuler([]);
    this.sidebar.setRulerState([], null, false);
  }

  private handleDropRangeRing(lat: number, lon: number): void {
    const num =
      this.state.tacticalGraphics.filter((g) => g.kind === "range_ring")
        .length + 1;
    const graphic: TacticalGraphic = {
      kind: "range_ring",
      label: `RING ${num}`,
      points: [[Number(lat.toFixed(6)), Number(lon.toFixed(6))]],
      radii_km: [...this.defaultRingRadiiKm],
      color: "#38BDF8",
    };
    this.state.tacticalGraphics = [...this.state.tacticalGraphics, graphic];
    this.map.updateTacticalGraphics(
      this.state.tacticalGraphics,
      this.sectorDraft
    );
    this.sidebar.openWaypointsSection();
    this.sidebar.renderTacticalGraphicsList();
    this.showToast(
      `Placed ${graphic.label} (${graphic.radii_km.join(", ")} km)`,
      "info"
    );
  }

  private handleSectorClick(lat: number, lon: number): void {
    this.sectorDraft = [
      ...this.sectorDraft,
      [Number(lat.toFixed(6)), Number(lon.toFixed(6))],
    ];
    this.sidebar.setSectorDraftCount(this.sectorDraft.length);
    this.map.updateTacticalGraphics(
      this.state.tacticalGraphics,
      this.sectorDraft
    );
  }

  private handleSectorFinish(): void {
    if (this.sectorDraft.length < 3) {
      this.showToast("Click at least 3 vertices to finish an AOI sector", "info");
      return;
    }
    const num =
      this.state.tacticalGraphics.filter((g) => g.kind === "sector").length + 1;
    const graphic: TacticalGraphic = {
      kind: "sector",
      label: `AOI ${num}`,
      points: [...this.sectorDraft],
      radii_km: [],
      color: "#F59E0B",
    };
    this.sectorDraft = [];
    this.state.tacticalGraphics = [...this.state.tacticalGraphics, graphic];
    this.sidebar.setSectorDraftCount(0);
    this.sidebar.renderTacticalGraphicsList();
    this.map.updateTacticalGraphics(this.state.tacticalGraphics, []);
    this.setInteractionMode("pan");
    this.showToast(`Created sector polygon "${graphic.label}"`, "success");
  }

  private handleSectorCancel(): void {
    this.sectorDraft = [];
    this.sidebar.setSectorDraftCount(0);
    this.map.updateTacticalGraphics(this.state.tacticalGraphics, []);
    if (this.interactionMode === "sector") {
      this.setInteractionMode("pan");
    }
  }

  private handleTacticalLabelEdit(index: number, label: string): void {
    const existing = this.state.tacticalGraphics[index];
    if (!existing) return;
    const next = [...this.state.tacticalGraphics];
    next[index] = { ...existing, label };
    this.state.tacticalGraphics = next;
    this.map.updateTacticalGraphics(
      this.state.tacticalGraphics,
      this.sectorDraft
    );
  }

  private handleTacticalDelete(index: number): void {
    this.state.tacticalGraphics = this.state.tacticalGraphics.filter(
      (_, i) => i !== index
    );
    this.map.updateTacticalGraphics(
      this.state.tacticalGraphics,
      this.sectorDraft
    );
    this.sidebar.renderTacticalGraphicsList();
  }

  private handleTacticalClear(): void {
    this.sectorDraft = [];
    this.state.tacticalGraphics = [];
    this.sidebar.setSectorDraftCount(0);
    this.map.updateTacticalGraphics([], []);
    this.sidebar.renderTacticalGraphicsList();
  }

  // ---------- RF Point-to-Point Link Profile (A ↔ B) ----------

  private handleRFLinkMapClick(lat: number, lon: number): void {
    const ptLat = Number(lat.toFixed(6));
    const ptLon = Number(lon.toFixed(6));

    if (!this.rfPendingFrom) {
      const linkNum = this.rfLinkSeq;
      this.rfPendingFrom = { lat: ptLat, lon: ptLon, label: `A${linkNum}` };
      this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);
      this.sidebar.setInteractionMode("rf-link", true);
      this.updateMapHintForMode();
      return;
    }

    const from = this.rfPendingFrom;
    this.rfPendingFrom = null;
    const seq = this.rfLinkSeq++;
    const d = this.state.rfLinkDefaults;
    const spec: RFLinkSpec = {
      from_lat: from.lat,
      from_lon: from.lon,
      to_lat: ptLat,
      to_lon: ptLon,
      from_height_m: d.from_height_m,
      to_height_m: d.to_height_m,
      freq_mhz: d.freq_mhz,
      k_factor: 1.333,
      from_label: from.label,
      to_label: `B${seq}`,
    };
    this.addAndComputeRFLink(spec);
    this.setInteractionMode("pan");
  }

  private handleRFLinkUseObservers(): void {
    if (this.state.viewsheds.length < 2) return;
    const o1 = this.state.viewsheds[0]!.observer;
    const o2 = this.state.viewsheds[1]!.observer;
    const d = this.state.rfLinkDefaults;
    this.addAndComputeRFLink({
      from_lat: o1.lat,
      from_lon: o1.lon,
      to_lat: o2.lat,
      to_lon: o2.lon,
      from_height_m: o1.observer_height_m,
      to_height_m: o2.observer_height_m,
      freq_mhz: d.freq_mhz,
      k_factor: 1.333,
      from_label: o1.label || "OBS1",
      to_label: o2.label || "OBS2",
    });
  }

  private addAndComputeRFLink(spec: RFLinkSpec): void {
    const id = `rf-${this.rfLinkSeq++}`;
    const entry: RFLinkEntry = {
      id,
      spec,
      loading: true,
      error: null,
      profile: null,
    };
    this.state.rfLinks = [...this.state.rfLinks, entry];
    if (!this.state.include_rf_link_card) {
      this.state.include_rf_link_card = true;
    }
    this.sidebar.openViewshedSection();
    this.sidebar.syncControlsFromState();
    this.map.updateRFLinks(this.state.rfLinks, null);
    void this.computeRFLinkEntry(id);
  }

  private async computeRFLinkEntry(id: string): Promise<void> {
    const entry = this.state.rfLinks.find((e) => e.id === id);
    if (!entry) return;

    this.rfLinkAborts.get(id)?.abort();
    const controller = new AbortController();
    this.rfLinkAborts.set(id, controller);

    this.state.rfLinks = this.state.rfLinks.map((e) =>
      e.id === id ? { ...e, loading: true, error: null } : e
    );
    this.sidebar.renderRFLinksList();
    this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);

    try {
      const profile = await fetchRFLinkProfile(
        entry.spec,
        this.state.dem_source,
        this.state.pack_id || null,
        controller.signal
      );
      if (controller.signal.aborted) return;

      this.state.rfLinks = this.state.rfLinks.map((e) =>
        e.id === id ? { ...e, loading: false, error: null, profile } : e
      );
      this.sidebar.renderRFLinksList();
      this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "RF link profile failed";
      this.state.rfLinks = this.state.rfLinks.map((e) =>
        e.id === id ? { ...e, loading: false, error: msg } : e
      );
      this.sidebar.renderRFLinksList();
      this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);
      this.showToast(`RF link error: ${msg}`, "error");
    } finally {
      if (this.rfLinkAborts.get(id) === controller) {
        this.rfLinkAborts.delete(id);
      }
    }
  }

  private handleRFLinkDelete(id: string): void {
    this.rfLinkAborts.get(id)?.abort();
    this.rfLinkAborts.delete(id);
    this.state.rfLinks = this.state.rfLinks.filter((e) => e.id !== id);
    if (this.state.rfLinks.length === 0) {
      this.state.include_rf_link_card = false;
    }
    this.sidebar.syncControlsFromState();
    this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);
  }

  private handleRFLinksClear(): void {
    for (const ctrl of this.rfLinkAborts.values()) ctrl.abort();
    this.rfLinkAborts.clear();
    this.rfPendingFrom = null;
    this.state.rfLinks = [];
    this.state.include_rf_link_card = false;
    this.sidebar.syncControlsFromState();
    this.map.updateRFLinks([], null);
  }

  // ---------- RF / LOS Viewshed Tool ----------

  private handleAddViewshed(lat: number, lon: number): void {
    const seq = this.viewshedSeq++;
    const id = `vs-${seq}`;
    const d = this.state.viewshedDefaults;
    const entry: ViewshedEntry = {
      id,
      observer: {
        label: `OBS${seq}`,
        lat: Number(lat.toFixed(6)),
        lon: Number(lon.toFixed(6)),
        observer_height_m: d.observer_height_m,
        target_height_m: d.target_height_m,
        max_distance_m: d.max_distance_km * 1000,
      },
      loading: true,
      error: null,
      preview: null,
    };

    this.state.viewsheds = [...this.state.viewsheds, entry];
    this.sidebar.openViewshedSection();
    this.sidebar.renderViewshedsList();
    this.map.updateViewsheds(this.state.viewsheds);
    this.syncUrlFromState();
    void this.computeViewshedEntry(id);
  }

  private handleViewshedEdit(
    id: string,
    patch: {
      label?: string;
      observer_height_m?: number;
      target_height_m?: number;
      max_distance_m?: number;
    },
    recompute: boolean
  ): void {
    this.state.viewsheds = this.state.viewsheds.map((e) =>
      e.id === id ? { ...e, observer: { ...e.observer, ...patch } } : e
    );
    this.syncUrlFromState();
    if (recompute) {
      void this.computeViewshedEntry(id);
    } else {
      this.map.updateViewsheds(this.state.viewsheds);
    }
  }

  private async computeViewshedEntry(id: string): Promise<void> {
    const entry = this.state.viewsheds.find((e) => e.id === id);
    if (!entry) return;

    this.viewshedAborts.get(id)?.abort();
    const controller = new AbortController();
    this.viewshedAborts.set(id, controller);

    this.state.viewsheds = this.state.viewsheds.map((e) =>
      e.id === id ? { ...e, loading: true, error: null } : e
    );
    this.sidebar.renderViewshedsList();

    try {
      const preview = await fetchViewshedPreview(
        entry.observer,
        this.state.dem_source,
        this.state.pack_id || null,
        controller.signal
      );
      if (controller.signal.aborted) return;

      this.state.viewsheds = this.state.viewsheds.map((e) =>
        e.id === id ? { ...e, loading: false, error: null, preview } : e
      );
      this.sidebar.renderViewshedsList();
      this.map.updateViewsheds(this.state.viewsheds);
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Viewshed computation failed";
      this.state.viewsheds = this.state.viewsheds.map((e) =>
        e.id === id ? { ...e, loading: false, error: msg } : e
      );
      this.sidebar.renderViewshedsList();
      this.showToast(`Viewshed error: ${msg}`, "error");
    } finally {
      if (this.viewshedAborts.get(id) === controller) {
        this.viewshedAborts.delete(id);
      }
    }
  }

  private handleViewshedDelete(id: string): void {
    this.viewshedAborts.get(id)?.abort();
    this.viewshedAborts.delete(id);
    this.state.viewsheds = this.state.viewsheds.filter((e) => e.id !== id);
    this.sidebar.renderViewshedsList();
    this.map.updateViewsheds(this.state.viewsheds);
    this.syncUrlFromState();
  }

  private handleViewshedsClear(): void {
    for (const ctrl of this.viewshedAborts.values()) ctrl.abort();
    this.viewshedAborts.clear();
    this.state.viewsheds = [];
    this.sidebar.renderViewshedsList();
    this.map.updateViewsheds([]);
    this.syncUrlFromState();
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
    this.sidebar.setFormError(null);
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

  // ---------- Drawers, Footprint, Health, Packs & Jobs ----------

  private syncRightDrawerShellClass(): void {
    this.shellEl.classList.toggle("drawer-open", this.drawerOpen);
    this.shellEl.classList.toggle("projects-open", this.projectsOpen);
    this.shellEl.classList.toggle(
      "right-drawer-open",
      this.drawerOpen || this.projectsOpen
    );
  }

  private setDrawerOpen(open: boolean): void {
    this.drawerOpen = open;
    if (open && this.projectsOpen) {
      this.projectsOpen = false;
      this.telemetry.updateProjectsOpen(false);
    }
    this.syncRightDrawerShellClass();
    this.updateQueueBadge();
  }

  private setProjectsOpen(open: boolean): void {
    this.projectsOpen = open;
    if (open && this.drawerOpen) {
      this.drawerOpen = false;
      this.updateQueueBadge();
    }
    this.syncRightDrawerShellClass();
    this.telemetry.updateProjectsOpen(open);
    if (open) {
      this.projectsDrawer.prepareSuggestedName();
      void this.projectsDrawer.refresh();
    }
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
          grid_cols: this.state.outputs.pdf ? this.state.grid_cols : 1,
          grid_rows: this.state.outputs.pdf ? this.state.grid_rows : 1,
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
      this.latestFootprint = null;
      this.telemetry.updateFootprint(null);
      this.map.clearFootprint();
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
            : "No offline packs installed",
          "info"
        );
      }
    } catch {
      if (notify) {
        this.showToast(
          "Could not refresh offline packs. Your selection is unchanged.",
          "error"
        );
      }
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
      nz_overlays: [...this.state.nz_overlays],
      ca_overlays: [...this.state.ca_overlays],
      us_overlays: [...this.state.us_overlays],
      carto: { ...this.state.carto },
      outputs: { ...this.state.outputs },
      grid_cols: this.state.stripPlan ? 1 : this.state.grid_cols,
      grid_rows: this.state.stripPlan ? 1 : this.state.grid_rows,
      waypoints: this.state.waypoints.map((wp) => ({ ...wp })),
      viewsheds: this.state.viewsheds.map((v) => ({ ...v.observer })),
      rf_links: this.state.rfLinks.map((l) => ({ ...l.spec })),
      tactical_graphics: this.state.tacticalGraphics.map((g) => ({
        ...g,
        points: g.points.map((p) => [p[0], p[1]]),
        radii_km: [...g.radii_km],
      })),
      include_calibration_page:
        this.state.outputs.pdf && this.state.include_calibration_page,
      include_route_card:
        this.state.outputs.pdf && this.state.include_route_card,
      include_elevation_profile:
        this.state.outputs.pdf && this.state.include_elevation_profile,
      include_atlas_index:
        this.state.outputs.pdf && this.state.include_atlas_index,
      include_rf_link_card:
        this.state.outputs.pdf && this.state.include_rf_link_card,
      show_fold_marks: this.state.show_fold_marks,
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
