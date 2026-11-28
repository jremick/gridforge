import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-sans/latin-700.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "@fontsource/ibm-plex-mono/latin-600.css";
import "@fontsource/ibm-plex-mono/latin-700.css";
import "./styles.css";
import {
  ICON_FIT,
  ICON_LAYERS,
  ICON_LOS,
  ICON_MINUS,
  ICON_PLUS,
  ICON_RING,
  ICON_RULER,
  ICON_SECTOR,
  ICON_WAYPOINT,
} from "./icons";
import {
  ApiError,
  cancelJob,
  createShareSnapshot,
  deleteJob,
  fetchBasemapInfo,
  fetchFootprint,
  fetchHealth,
  fetchJobs,
  fetchPacks,
  fetchRFLinkProfile,
  fetchRouteMetrics,
  fetchRulerMetrics,
  fetchSession,
  fetchShareSnapshot,
  fetchStripPlan,
  fetchViewshedPreview,
  logoutSession,
  submitJob,
} from "./api";
import type { AdminModal } from "./components/adminModal";
import { AuthModal } from "./components/authModal";
import { JobsDrawer } from "./components/jobsDrawer";
import type { PacksModal } from "./components/packsModal";
import { ProjectsDrawer } from "./components/projectsDrawer";
import { SidebarPanel } from "./components/sidebar";
import { TelemetryBar } from "./components/telemetry";
import {
  DEFAULT_BASEMAP,
  buildDirectShareUrl,
  hydrateStateFromSearch,
  hydrateStateFromSnapshot,
  stripOneTimeLaunchParams,
} from "./deeplink";
import { GridForgeMap } from "./map";
import {
  BASEMAP_OPTIONS,
  DEFAULT_MAP_ZOOM,
  buildCliCommand,
  createInitialFormState,
  escapeHtml,
  formatCoords,
} from "./presets";
import {
  clampFormStateToCapabilities,
  classifyApiError,
  createDefaultNoneSession,
  extractAuthErrorFromSearch,
  extractInviteTokenFromSearch,
  isGuestRenderPaused,
} from "./session";
import {
  DEFAULT_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  SIDEBAR_MOBILE_BREAKPOINT,
  clampSidebarWidth,
  getMaxSidebarWidthForViewport,
  loadSidebarPrefs,
  saveSidebarPrefs,
} from "./sidebarPrefs";
import {
  applyStudioTheme,
  loadStudioTheme,
  saveStudioTheme,
  type StudioTheme,
} from "./themePrefs";
import { TurnstileController } from "./turnstile";
import type {
  BasemapId,
  CreateJobPayload,
  FootprintResponse,
  GeocodeResult,
  JobRecord,
  LocationPreset,
  MapInteractionMode,
  RFLinkEntry,
  RFLinkSpec,
  RulerMetricsResponse,
  SavedProject,
  SessionResponse,
  TacticalGraphic,
  ViewshedEntry,
  WaypointCategory,
} from "./types";

const FOOTPRINT_DEBOUNCE_MS = 180;
const ROUTE_METRICS_DEBOUNCE_MS = 150;
const ACTIVE_JOBS_POLL_MS = 2000;
const IDLE_JOBS_POLL_MS = 10000;
const HEALTH_POLL_MS = 20000;
const DEFAULT_RING_RADII_KM = [1, 3, 5];

const KNOWN_AUTH_ERRORS: Record<string, string> = {
  access_denied: "Google sign-in was cancelled.",
  oauth_failed: "Google sign-in failed. Please try again.",
  invite_required: "An admin invite link is required to sign in.",
  account_disabled: "This account has been disabled by an administrator.",
  session_expired: "Your session expired. Please sign in again.",
};

type ApiErrorContext =
  | "job"
  | "viewshed"
  | "rf"
  | "geocode"
  | "strip"
  | "ruler"
  | "packs"
  | "projects"
  | "shares"
  | "jobs-poll"
  | "admin"
  | "general";

class GridForgeApp {
  private readonly state = createInitialFormState();
  private readonly shellEl: HTMLElement;
  private readonly telemetry: TelemetryBar;
  private readonly sidebar: SidebarPanel;
  private readonly drawer: JobsDrawer;
  private readonly projectsDrawer: ProjectsDrawer;
  private readonly packsModalEl: HTMLElement;
  private packsModal: PacksModal | null = null;
  private readonly authModal: AuthModal;
  private readonly adminModalEl: HTMLElement;
  private adminModal: AdminModal | null = null;
  private readonly turnstile: TurnstileController;
  private readonly map: GridForgeMap;
  private readonly basemapSwitcherEl: HTMLElement;
  private readonly basemapSelectEl: HTMLSelectElement;
  private readonly sidebarScrimEl: HTMLElement;
  private readonly offlineReadyBadgeEl: HTMLButtonElement;
  private studioTheme: StudioTheme = loadStudioTheme();
  private readonly globalStatusBannerEl: HTMLElement;
  private readonly footprintBannerEl: HTMLElement;
  private readonly mapHintEl: HTMLElement;
  private readonly mapHintTextEl: HTMLElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly sidebarEl: HTMLElement;
  private readonly sidebarResizerEl: HTMLElement;
  private readonly toolBarButtons: NodeListOf<HTMLButtonElement>;

  private session: SessionResponse = createDefaultNoneSession();
  private sessionLoaded = false;
  private sessionRefreshInFlight: Promise<SessionResponse | null> | null = null;
  private sessionRefreshAbort: AbortController | null = null;
  private sessionMutationInFlight: Promise<SessionResponse> | null = null;
  private sessionChannel: BroadcastChannel | null = null;
  private workspaceRequests = new AbortController();
  private pendingInviteToken: string | null;
  private pendingAuthError: string | null;
  private sidebarCollapsed = false;
  private sidebarWidth = DEFAULT_SIDEBAR_WIDTH;
  private sidebarPrefOwnerId: string | null = null;
  private drawerOpen = false;
  private projectsOpen = false;
  private interactionMode: MapInteractionMode = "pan";
  private activeBasemap: BasemapId = DEFAULT_BASEMAP;
  private hasExplicitUrlZoom = false;
  private pendingOfflineBasemapFromUrl = false;
  private activeShareId: string | null = null;
  private latestFootprint: FootprintResponse | null = null;
  private jobs: JobRecord[] = [];
  private readonly initialFlyToResolved: boolean;

  private rulerPoints: { lat: number; lon: number }[] = [];
  private rulerMetrics: RulerMetricsResponse | null = null;
  private defaultRingRadiiKm = DEFAULT_RING_RADII_KM;
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
    const launchUrl = new URL(window.location.href);
    this.pendingInviteToken = extractInviteTokenFromSearch(launchUrl.search);
    const rawAuthError = extractAuthErrorFromSearch(launchUrl.search);
    this.pendingAuthError = rawAuthError
      ? (KNOWN_AUTH_ERRORS[rawAuthError.toLowerCase()] ??
          "Sign-in could not be completed. Please try again.")
      : null;
    const urlHydration = hydrateStateFromSearch(launchUrl.search, this.state);
    // Use inbound map settings and authentication parameters once.
    if (stripOneTimeLaunchParams(launchUrl, urlHydration.remainingSearch)) {
      window.history.replaceState(window.history.state, "", launchUrl);
    }
    this.initialFlyToResolved = urlHydration.needsFlyToResolvedCenter;
    this.activeShareId = urlHydration.shareId;
    this.hasExplicitUrlZoom = urlHydration.zoom !== null;
    if (urlHydration.hasCustomBasemap) {
      if (urlHydration.basemap === "offline") {
        this.pendingOfflineBasemapFromUrl = true;
        this.activeBasemap = DEFAULT_BASEMAP;
      } else {
        this.activeBasemap = urlHydration.basemap;
      }
    }
    this.viewshedSeq = this.state.viewsheds.length + 1;
    this.rfLinkSeq = this.state.rfLinks.length + 1;

    applyStudioTheme(this.studioTheme);

    root.innerHTML = `
      <div class="gf-shell" data-role="shell">
        <header data-role="topbar"></header>
        <main class="gf-workspace">
          <div class="gf-map-container" data-role="map"></div>

          <aside id="gf-studio-sidebar" data-role="sidebar"></aside>
          <div
            class="gf-sidebar-resizer"
            data-role="sidebar-resizer"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize studio sidebar"
            aria-controls="gf-studio-sidebar"
            aria-valuemin="${MIN_SIDEBAR_WIDTH}"
            aria-valuemax="560"
            aria-valuenow="${DEFAULT_SIDEBAR_WIDTH}"
            tabindex="0"
            title="Drag to resize sidebar · Double-click to reset"
          ></div>
          <div class="gf-sidebar-scrim" data-role="sidebar-scrim" aria-hidden="true"></div>

          <div class="gf-map-toolbar">
            <div class="gf-toolbar-row">
              <button
                type="button"
                class="gf-offline-ready-pill hidden"
                data-role="offline-ready-pill"
                title="External tiles failed. Click to switch to local Offline MBTiles basemap"
              >Offline Basemap Ready</button>
              <div class="gf-basemap-switcher" data-role="basemap-switcher" role="group" aria-label="Basemap switcher">
                ${BASEMAP_OPTIONS.map(
                  (b) =>
                    `<button type="button" class="gf-basemap-btn ${b.id === this.activeBasemap ? "active" : ""}" data-basemap="${escapeHtml(b.id)}">${escapeHtml(b.label)}</button>`
                ).join("")}
              </div>
              <label class="gf-basemap-select-wrap" aria-label="Basemap">
                ${ICON_LAYERS}
                <select class="gf-basemap-select" data-role="basemap-select" aria-label="Basemap">
                  ${BASEMAP_OPTIONS.map(
                    (b) =>
                      `<option value="${escapeHtml(b.id)}" ${b.id === this.activeBasemap ? "selected" : ""}>${escapeHtml(b.label)}</option>`
                  ).join("")}
                </select>
              </label>
              <div class="gf-map-actions">
                <button type="button" class="gf-map-icon-btn" data-map-action="fit" title="Fit sheet footprint (F)">${ICON_FIT}<span>Fit</span></button>
                <button type="button" class="gf-map-icon-btn gf-map-icon-only" data-map-action="zoom-in" aria-label="Zoom in" title="Zoom in">${ICON_PLUS}</button>
                <button type="button" class="gf-map-icon-btn gf-map-icon-only" data-map-action="zoom-out" aria-label="Zoom out" title="Zoom out">${ICON_MINUS}</button>
                <button type="button" class="gf-map-icon-btn gf-map-icon-only" data-map-action="north" aria-label="Reset north" title="Reset north">N</button>
              </div>
            </div>
            <div class="gf-toolbar-row">
              <div class="gf-map-actions gf-tactical-bar" role="group" aria-label="Tactical map tools">
                <button type="button" class="gf-map-icon-btn gf-markup-btn" data-map-tool="markup" aria-pressed="false" title="Drop waypoints on map (M)">${ICON_WAYPOINT}<span>WP</span></button>
                <button type="button" class="gf-map-icon-btn gf-ruler-btn" data-map-tool="ruler" aria-pressed="false" title="Measure distance, magnetic bearing &amp; grade (R)">${ICON_RULER}<span>Ruler</span></button>
                <button type="button" class="gf-map-icon-btn gf-ring-btn" data-map-tool="ring" aria-pressed="false" title="Drop concentric range rings on map">${ICON_RING}<span>Ring</span></button>
                <button type="button" class="gf-map-icon-btn gf-sector-btn" data-map-tool="sector" aria-pressed="false" title="Draw shaded AOI sector polygon">${ICON_SECTOR}<span>Sector</span></button>
                <button type="button" class="gf-map-icon-btn gf-los-btn" data-map-tool="los" aria-pressed="false" title="Drop RF / LOS viewshed observer (V)">${ICON_LOS}<span>LOS</span></button>
              </div>
            </div>
            <div class="gf-footprint-banner hidden" data-role="footprint-banner"></div>
          </div>

          <div class="gf-global-status-banner hidden" data-role="global-status-banner" role="status"></div>

          <div class="gf-map-hint" data-role="map-hint">
            <span data-role="map-hint-text">Click map or drag reticle · F to fit</span>
            <span class="mono" data-role="cursor-coords">—</span>
          </div>

          <aside data-role="drawer"></aside>
          <aside data-role="projects-drawer"></aside>
        </main>
        <div data-role="packs-modal"></div>
        <div data-role="auth-modal"></div>
        <div data-role="admin-modal"></div>
        <div class="gf-toast-container" data-role="toasts"></div>
      </div>
    `;

    this.shellEl = this.q(root, "[data-role='shell']");
    this.basemapSwitcherEl = this.q(root, "[data-role='basemap-switcher']");
    this.basemapSelectEl = this.q<HTMLSelectElement>(
      root,
      "[data-role='basemap-select']"
    );
    this.sidebarScrimEl = this.q(root, "[data-role='sidebar-scrim']");
    this.offlineReadyBadgeEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='offline-ready-pill']"
    );
    this.globalStatusBannerEl = this.q(
      root,
      "[data-role='global-status-banner']"
    );
    this.footprintBannerEl = this.q(root, "[data-role='footprint-banner']");
    this.mapHintEl = this.q(root, "[data-role='map-hint']");
    this.mapHintTextEl = this.q(root, "[data-role='map-hint-text']");
    this.cursorCoordsEl = this.q(root, "[data-role='cursor-coords']");
    this.toastContainerEl = this.q(root, "[data-role='toasts']");
    this.sidebarEl = this.q(root, "[data-role='sidebar']");
    this.sidebarResizerEl = this.q(root, "[data-role='sidebar-resizer']");
    this.toolBarButtons = root.querySelectorAll<HTMLButtonElement>(
      "[data-map-tool]"
    );

    this.telemetry = new TelemetryBar(this.q(root, "[data-role='topbar']"), {
      onToggleSidebar: () => this.setSidebarCollapsed(!this.sidebarCollapsed),
      onToggleTheme: () => this.toggleStudioTheme(),
      onToggleDrawer: () => this.setDrawerOpen(!this.drawerOpen),
      onToggleProjects: () => this.setProjectsOpen(!this.projectsOpen),
      onCopyMgrs: (mgrs) => this.copyText(mgrs, `Copied ${mgrs}`),
      onShareOpen: () => this.refreshSharePopoverSummary(),
      onCopyDirectLink: () => this.handleCopyDirectLink(),
      onCreateShortLink: () => void this.handleCreateShortShareLink(),
      onCopyShareUrl: (url) =>
        this.copyText(url, "Share link copied to clipboard"),
      onOpenAuth: () => this.authModal.open(),
      onSignOut: () => void this.handleSignOut(),
      onOpenAdmin: () => void this.openAdminModal(),
    });

    this.packsModalEl = this.q(root, "[data-role='packs-modal']");
    this.adminModalEl = this.q(root, "[data-role='admin-modal']");

    this.authModal = new AuthModal(this.q(root, "[data-role='auth-modal']"), {
      changeSession: (request) => this.changeSession(request),
      onOpenAdmin: () => void this.openAdminModal(),
      onToast: (msg, kind) => this.showToast(msg, kind),
    });

    this.projectsDrawer = new ProjectsDrawer(
      this.q(root, "[data-role='projects-drawer']"),
      {
        onClose: () => this.setProjectsOpen(false),
        onLoadProject: (proj) => this.loadProject(proj),
        onShareProject: (proj) => this.handleShareProject(proj),
        getCurrentSnapshot: () => this.buildProjectSnapshot(),
        onToast: (msg, kind) => this.showToast(msg, kind),
        onApiError: (err) => {
          this.handleApiError(err, "projects", "Project request failed");
        },
      }
    );

    this.sidebar = new SidebarPanel(
      this.q(root, "[data-role='sidebar']"),
      this.state,
      {
        onCenterSubmit: (centerText) => {
          this.clearStripPlanInternal(false);
          this.state.center = centerText;
          this.workspaceEdited();
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
          this.workspaceEdited();
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
          this.workspaceEdited();
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
          } else {
            this.workspaceEdited();
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
        onManagePacks: () => void this.openPacksModal(),
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
        onApiError: (err, source) => {
          const msg = this.handleApiError(
            err,
            source,
            "Place search temporarily unavailable"
          );
          this.showToast(msg, "error");
        },
        onOpenAuth: () => this.authModal.open(),
      },
      urlHydration.hasCustomCenter ? null : "blue-mountains"
    );

    this.turnstile = new TurnstileController({
      container: this.sidebar.getTurnstileContainer(),
      onInteractiveNeeded: () => {
        if (this.sidebarCollapsed) {
          this.setSidebarCollapsed(false, { persist: false });
        }
      },
    });

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
          this.workspaceEdited();
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
        onVectorBasemapError: () => {
          const fallback: BasemapId = this.basemapSwitcherEl.querySelector(
            "[data-basemap='offline']"
          )
            ? "offline"
            : "opentopo";
          this.selectBasemap(fallback);
          this.showToast(
            "Vector HD tiles unavailable; switched back to raster basemap",
            "info"
          );
        },
      },
      {
        initialLat: this.state.lat,
        initialLon: this.state.lon,
        initialZoom: urlHydration.zoom ?? DEFAULT_MAP_ZOOM,
        initialBasemap: this.activeBasemap,
      }
    );

    this.telemetry.updateTheme(this.studioTheme);
    this.map.setTheme(this.studioTheme);

    this.restoreSidebarPrefs(null);
    this.bindChromeEvents(root);
    void this.bootstrap();
  }

  private q<T extends HTMLElement = HTMLElement>(
    root: ParentNode,
    selector: string
  ): T {
    const el = root.querySelector<T>(selector);
    if (!el) throw new Error(`App element not found: ${selector}`);
    return el;
  }

  private getEffectiveSidebarWidth(): number {
    return clampSidebarWidth(this.sidebarWidth, window.innerWidth);
  }

  private applySidebarLayout(options: { resizeMap?: boolean } = {}): void {
    const vw = window.innerWidth;
    const effectiveWidth = clampSidebarWidth(this.sidebarWidth, vw);
    const maxWidth = getMaxSidebarWidthForViewport(vw);

    this.shellEl.style.setProperty("--sidebar-w", `${effectiveWidth}px`);
    this.shellEl.classList.toggle("sidebar-collapsed", this.sidebarCollapsed);
    this.sidebarEl.setAttribute("aria-hidden", String(this.sidebarCollapsed));
    if ("inert" in this.sidebarEl) {
      (this.sidebarEl as HTMLElement & { inert: boolean }).inert =
        this.sidebarCollapsed;
    }

    this.sidebarResizerEl.setAttribute("aria-valuemin", String(MIN_SIDEBAR_WIDTH));
    this.sidebarResizerEl.setAttribute("aria-valuemax", String(maxWidth));
    this.sidebarResizerEl.setAttribute("aria-valuenow", String(effectiveWidth));
    this.sidebarResizerEl.hidden = this.sidebarCollapsed;
    this.telemetry.updateSidebarCollapsed(this.sidebarCollapsed);

    if (options.resizeMap) {
      window.setTimeout(() => this.map.resize(), 240);
    }
  }

  private restoreSidebarPrefs(userId: string | null): void {
    this.sidebarPrefOwnerId = userId;
    const prefs = loadSidebarPrefs(userId, window.innerWidth);
    this.sidebarWidth = prefs.width;
    this.sidebarCollapsed = prefs.collapsed;
    this.applySidebarLayout({ resizeMap: false });
  }

  private persistSidebarPrefs(): void {
    saveSidebarPrefs(
      { width: this.sidebarWidth, collapsed: this.sidebarCollapsed },
      this.sidebarPrefOwnerId,
      window.innerWidth
    );
  }

  private setSidebarCollapsed(
    collapsed: boolean,
    options: { persist?: boolean } = {}
  ): void {
    if (this.sidebarCollapsed === collapsed) return;
    this.sidebarCollapsed = collapsed;
    this.applySidebarLayout({ resizeMap: true });
    if (options.persist !== false) {
      this.persistSidebarPrefs();
    }
  }

  private setSidebarWidth(
    nextWidth: number,
    options: { persist?: boolean; resizeMap?: boolean } = {}
  ): void {
    const clamped = clampSidebarWidth(nextWidth, window.innerWidth);
    this.sidebarWidth = clamped;
    this.applySidebarLayout({ resizeMap: Boolean(options.resizeMap) });
    if (options.persist) {
      this.persistSidebarPrefs();
    }
  }

  private bindChromeEvents(root: HTMLElement): void {
    let activePointerId: number | null = null;
    let dragStartX = 0;
    let dragStartWidth = this.sidebarWidth;

    const finishDrag = (): void => {
      if (activePointerId === null) return;
      if (this.sidebarResizerEl.hasPointerCapture?.(activePointerId)) {
        try {
          this.sidebarResizerEl.releasePointerCapture(activePointerId);
        } catch (err) {
          console.debug("Pointer capture release skipped:", err);
        }
      }
      activePointerId = null;
      this.shellEl.classList.remove("is-resizing");
      this.sidebarResizerEl.classList.remove("dragging");
      this.persistSidebarPrefs();
      this.map.resize();
    };

    this.sidebarResizerEl.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || this.sidebarCollapsed) return;
      e.preventDefault();
      activePointerId = e.pointerId;
      dragStartX = e.clientX;
      dragStartWidth = this.getEffectiveSidebarWidth();
      this.shellEl.classList.add("is-resizing");
      this.sidebarResizerEl.classList.add("dragging");
      try {
        this.sidebarResizerEl.setPointerCapture?.(e.pointerId);
      } catch (err) {
        console.debug("Pointer capture unavailable:", err);
      }
    });

    this.sidebarResizerEl.addEventListener("pointermove", (e) => {
      if (activePointerId === null || e.pointerId !== activePointerId) return;
      const delta = e.clientX - dragStartX;
      this.setSidebarWidth(dragStartWidth + delta, {
        persist: false,
        resizeMap: false,
      });
    });

    this.sidebarResizerEl.addEventListener("pointerup", (e) => {
      if (activePointerId !== null && e.pointerId === activePointerId) {
        finishDrag();
      }
    });

    this.sidebarResizerEl.addEventListener("pointercancel", (e) => {
      if (activePointerId !== null && e.pointerId === activePointerId) {
        finishDrag();
      }
    });

    this.sidebarResizerEl.addEventListener("dblclick", () => {
      this.setSidebarWidth(DEFAULT_SIDEBAR_WIDTH, {
        persist: true,
        resizeMap: true,
      });
    });

    this.sidebarResizerEl.addEventListener("keydown", (e) => {
      if (this.sidebarCollapsed) return;
      const step = e.shiftKey ? 48 : 16;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        this.setSidebarWidth(this.getEffectiveSidebarWidth() - step, {
          persist: true,
          resizeMap: true,
        });
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        this.setSidebarWidth(this.getEffectiveSidebarWidth() + step, {
          persist: true,
          resizeMap: true,
        });
      } else if (e.key === "Home") {
        e.preventDefault();
        this.setSidebarWidth(DEFAULT_SIDEBAR_WIDTH, {
          persist: true,
          resizeMap: true,
        });
      }
    });

    window.addEventListener("resize", () => {
      this.applySidebarLayout({ resizeMap: false });
    });

    this.basemapSwitcherEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("[data-basemap]");
      if (!btn) return;
      const id = btn.dataset.basemap as BasemapId;
      this.selectBasemap(id);
      this.workspaceEdited();
    });

    this.basemapSelectEl.addEventListener("change", () => {
      const id = this.basemapSelectEl.value as BasemapId;
      this.selectBasemap(id);
      this.workspaceEdited();
    });

    this.sidebarScrimEl.addEventListener("click", () => {
      if (!this.sidebarCollapsed) {
        this.setSidebarCollapsed(true, { persist: false });
      }
    });

    this.offlineReadyBadgeEl.addEventListener("click", () => {
      this.selectBasemap("offline");
      this.workspaceEdited();
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
      if (this.sessionMutationInFlight) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      const inInput =
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        Boolean(target?.isContentEditable);

      if (e.key === "Escape") {
        if (this.adminModal?.opened) {
          this.adminModal.close();
        } else if (this.authModal.opened) {
          this.authModal.close();
        } else if (this.packsModal?.opened) {
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

      if (
        this.adminModal?.opened ||
        this.authModal.opened ||
        this.packsModal?.opened ||
        inInput
      ) {
        return;
      }

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
          this.setSidebarCollapsed(false, { persist: false });
        }
        this.sidebar.focusCenterInput();
      } else if (
        e.key === "[" &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        this.setSidebarCollapsed(!this.sidebarCollapsed);
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

  private toggleStudioTheme(): void {
    this.studioTheme = this.studioTheme === "dark" ? "light" : "dark";
    saveStudioTheme(this.studioTheme);
    applyStudioTheme(this.studioTheme);
    this.telemetry.updateTheme(this.studioTheme);
    this.map.setTheme(this.studioTheme);
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
    if (
      Array.from(this.basemapSelectEl.options).some((o) => o.value === id)
    ) {
      this.basemapSelectEl.value = id;
    }
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
    if (
      mode !== "pan" &&
      !this.sidebarCollapsed &&
      window.innerWidth <= SIDEBAR_MOBILE_BREAKPOINT
    ) {
      this.setSidebarCollapsed(true, { persist: false });
    }
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
    this.map.fitFootprint(
      this.sidebarCollapsed ? 24 : this.getEffectiveSidebarWidth()
    );
  }

  private async bootstrap(): Promise<void> {
    const { signal } = this.workspaceRequests;
    const initialShareId = this.activeShareId;
    const shareFetchPromise = initialShareId
      ? fetchShareSnapshot(initialShareId, signal).then(
          (snapshot) => ({ ok: true as const, snapshot }),
          (error: unknown) => ({ ok: false as const, error })
        )
      : null;

    if (!initialShareId) {
      this.sidebar.syncControlsFromState();
      if (this.state.waypoints.length > 0) {
        this.map.updateWaypoints(this.state.waypoints);
        this.scheduleRouteMetrics();
      }
      if (this.state.tacticalGraphics.length > 0) {
        this.map.updateTacticalGraphics(this.state.tacticalGraphics, []);
      }
      if (this.state.rfLinks.length > 0) {
        this.map.updateRFLinks(this.state.rfLinks, null);
      }

      this.requestFootprintImmediate({
        flyToResult: this.initialFlyToResolved,
        syncMarker: true,
        fitAfterLoad:
          !this.hasExplicitUrlZoom &&
          this.state.grid_cols * this.state.grid_rows > 1,
      });
      void this.loadBasemapInfo(this.state.pack_id || null);
    }

    const session = await this.refreshSession();
    void this.loadHealth();
    void this.loadPacks(false);
    if (session) {
      void this.pollJobsOnce({ sessionChecked: true });
    } else {
      this.scheduleNextJobsPoll();
    }

    window.setInterval(() => {
      if (!document.hidden) {
        void this.loadHealth();
      }
    }, HEALTH_POLL_MS);
    window.setInterval(() => {
      if (!document.hidden) {
        this.drawer.tickElapsed();
      }
    }, 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        void this.loadHealth();
        void this.pollJobsOnce();
      }
    });
    window.addEventListener("focus", () => {
      if (this.session.auth_mode === "hosted" && !document.hidden) {
        void this.refreshSession();
      }
    });
    window.addEventListener("pageshow", (event) => {
      if (event.persisted && this.session.auth_mode === "hosted") {
        void this.refreshSession({ force: true });
      }
    });

    if (shareFetchPromise) {
      const result = await shareFetchPromise;
      if (signal.aborted || this.activeShareId !== initialShareId) return;
      if (result.ok) {
        const labelName = result.snapshot.name.trim() || result.snapshot.share_id;
        this.applySnapshotState(result.snapshot.state, {
          toastLabel: `Loaded shared map "${labelName}"`,
          preserveShareId: result.snapshot.share_id,
          flyToCenter: false,
        });
        return;
      }

      this.setShareLocation(null);
      const errMsg =
        result.error instanceof ApiError && result.error.status === 404
          ? "Shared map link not found or expired."
          : result.error instanceof Error
            ? result.error.message
            : "Failed to load shared map link.";
      this.showToast(errMsg, "error");
      this.sidebar.syncControlsFromState();
      this.requestFootprintImmediate({
        flyToResult: false,
        syncMarker: true,
        fitAfterLoad: false,
      });
      void this.loadBasemapInfo(this.state.pack_id || null);
      return;
    }

    if (this.state.viewsheds.length > 0 || this.state.rfLinks.length > 0) {
      this.sidebar.openViewshedSection();
      if (this.state.viewsheds.length > 0) {
        this.map.updateViewsheds(this.state.viewsheds);
        this.sidebar.renderViewshedsList();
      }
      if (this.state.rfLinks.length > 0) {
        this.map.updateRFLinks(this.state.rfLinks, null);
        this.sidebar.renderRFLinksList();
      }
    }
  }

  private async openPacksModal(): Promise<void> {
    if (!this.session.capabilities.packs_manage_allowed) return;
    if (!this.packsModal) {
      const { signal } = this.workspaceRequests;
      const { PacksModal } = await import("./components/packsModal");
      if (signal.aborted || !this.session.capabilities.packs_manage_allowed) return;
      this.packsModal ??= new PacksModal(this.packsModalEl, {
        onPacksUpdated: (packs) => {
          this.sidebar.setPacks(packs);
          void this.loadHealth();
        },
        onSelectPack: (packId) => {
          this.state.pack_id = packId;
          this.sidebar.syncControlsFromState();
          this.workspaceEdited();
          void this.loadBasemapInfo(packId);
          this.showToast(`Selected offline pack "${packId}"`, "success");
        },
        onBasemapRefresh: () => {
          void this.loadBasemapInfo(this.state.pack_id || null);
        },
        onToast: (msg, kind) => this.showToast(msg, kind),
        getCurrentBBoxString: () => this.map.getFootprintBBoxString(),
        onApiError: (err) => {
          this.handleApiError(err, "packs", "Offline pack request failed");
        },
      }, signal);
      this.packsModal.setManageAllowed(this.session.capabilities.packs_manage_allowed);
    }
    this.packsModal.open();
  }

  private async openAdminModal(): Promise<void> {
    if (this.session.role !== "admin") return;
    if (!this.adminModal) {
      const { signal } = this.workspaceRequests;
      const { AdminModal } = await import("./components/adminModal");
      if (signal.aborted || this.session.role !== "admin") return;
      this.adminModal ??= new AdminModal(this.adminModalEl, {
        getCurrentUserId: () => this.session.user?.user_id ?? null,
        onSettingsChanged: () => {
          void this.refreshSession();
        },
        onCopyText: (text, label) => this.copyText(text, label),
        onToast: (msg, kind) => this.showToast(msg, kind),
        onApiError: (err) => {
          this.handleApiError(err, "admin", "Admin request failed");
        },
      }, signal);
      this.adminModal.syncPublicPaused(this.session.quotas.public_renders_paused);
    }
    this.adminModal.open();
  }

  private refreshSession(
    options: { force?: boolean } = {}
  ): Promise<SessionResponse | null> {
    if (this.sessionMutationInFlight) {
      const pending = this.sessionMutationInFlight.catch(() => null);
      return options.force
        ? pending.then(() => this.refreshSession(options))
        : pending;
    }
    if (!options.force && this.sessionRefreshInFlight) {
      return this.sessionRefreshInFlight;
    }
    this.sessionRefreshAbort?.abort();
    const controller = new AbortController();
    this.sessionRefreshAbort = controller;
    const refresh = fetchSession(controller.signal)
      .catch((err: unknown) => {
        if (controller.signal.aborted) return null;
        if (!this.sessionLoaded && err instanceof ApiError && err.status === 404) {
          return createDefaultNoneSession();
        }
        console.warn("Failed to load session; keeping current session:", err);
        return null;
      })
      .then((next) => {
        if (!next || controller.signal.aborted) return null;
        this.applySession(next);
        return next;
      })
      .finally(() => {
        if (this.sessionRefreshAbort === controller) {
          this.sessionRefreshAbort = null;
          this.sessionRefreshInFlight = null;
        }
      });
    this.sessionRefreshInFlight = refresh;
    return refresh;
  }

  private sessionIdentityKey(session: SessionResponse): string {
    return [
      session.auth_mode,
      session.authenticated ? "1" : "0",
      session.role,
      session.user?.user_id ?? "",
      session.workspace_scope ?? "",
    ].join("|");
  }

  private changeSession(
    request: () => Promise<SessionResponse>
  ): Promise<SessionResponse> {
    if (this.sessionMutationInFlight) {
      return Promise.reject(new Error("An account change is already in progress."));
    }
    this.sessionRefreshAbort?.abort();
    this.sessionRefreshAbort = null;
    this.sessionRefreshInFlight = null;
    this.shellEl.inert = true;
    const change = Promise.resolve()
      .then(request)
      .then((next) => {
        this.applySession(next);
        this.sessionChannel?.postMessage({ scope: this.sessionIdentityKey(next) });
        return next;
      })
      .finally(() => {
        this.sessionMutationInFlight = null;
        this.shellEl.inert = false;
      });
    this.sessionMutationInFlight = change;
    return change;
  }

  private resetWorkspace(): void {
    this.workspaceRequests.abort();
    this.workspaceRequests = new AbortController();

    for (const timer of [this.footprintTimer, this.metricsTimer, this.jobsPollTimer]) {
      if (timer !== null) window.clearTimeout(timer);
    }
    this.footprintTimer = this.metricsTimer = this.jobsPollTimer = null;
    this.footprintAbort?.abort();
    this.stripAbort?.abort();
    this.metricsAbort?.abort();
    this.rulerAbort?.abort();
    this.footprintAbort = this.stripAbort = this.metricsAbort = this.rulerAbort = null;
    for (const controller of this.viewshedAborts.values()) controller.abort();
    for (const controller of this.rfLinkAborts.values()) controller.abort();
    this.viewshedAborts.clear();
    this.rfLinkAborts.clear();
    this.turnstile.unmount();

    Object.assign(this.state, createInitialFormState());
    this.latestFootprint = null;
    this.rulerPoints = [];
    this.rulerMetrics = null;
    this.sectorDraft = [];
    this.rfPendingFrom = null;
    this.defaultRingRadiiKm = DEFAULT_RING_RADII_KM;
    this.viewshedSeq = 1;
    this.rfLinkSeq = 1;
    this.pendingOfflineBasemapFromUrl = false;
    this.pendingInviteToken = this.pendingAuthError = null;
    this.setShareLocation(null);

    this.setInteractionMode("pan");
    this.map.resetWorkspace(this.state.lat, this.state.lon);
    this.selectBasemap(DEFAULT_BASEMAP);
    this.basemapSwitcherEl.querySelector("[data-basemap='offline']")?.remove();
    this.basemapSelectEl.querySelector("option[value='offline']")?.remove();
    this.offlineReadyBadgeEl.classList.add("hidden");
    this.sidebar.resetWorkspace();
    this.telemetry.resetWorkspace();
    this.cursorCoordsEl.textContent = "—";
    this.setFootprintError(null);

    this.jobs = [];
    this.drawer.setJobs([]);
    this.setDrawerOpen(false);
    this.projectsDrawer.clear();
    this.setProjectsOpen(false);
    this.authModal.clearAccountState();
    this.packsModal?.dispose();
    this.packsModal = null;
    this.adminModal?.dispose();
    this.adminModal = null;
    this.toastContainerEl.replaceChildren();
  }

  private applySession(next: SessionResponse): void {
    const prev = this.session;
    const isBootstrap = !this.sessionLoaded;
    // Preserve focus and in-progress form edits during routine session polls.
    if (!isBootstrap && JSON.stringify(prev) === JSON.stringify(next)) return;
    const tenantChanged =
      !isBootstrap &&
      (prev.auth_mode !== next.auth_mode ||
        prev.authenticated !== next.authenticated ||
        prev.role !== next.role ||
        (prev.user?.user_id ?? null) !== (next.user?.user_id ?? null) ||
        (prev.workspace_scope ?? null) !== (next.workspace_scope ?? null));

    if (tenantChanged) this.resetWorkspace();
    this.session = next;
    this.sessionLoaded = true;
    if (
      next.auth_mode === "hosted" &&
      !this.sessionChannel &&
      typeof BroadcastChannel === "function"
    ) {
      this.sessionChannel = new BroadcastChannel("gridforge-session");
      this.sessionChannel.addEventListener("message", (event: MessageEvent) => {
        const data = event.data as { scope?: string } | string | null;
        if (
          data &&
          typeof data === "object" &&
          typeof data.scope === "string" &&
          data.scope === this.sessionIdentityKey(this.session)
        ) {
          return;
        }
        void this.refreshSession({ force: true });
      });
      if (isBootstrap && next.authenticated) {
        this.sessionChannel.postMessage({ scope: this.sessionIdentityKey(next) });
      }
    }
    const nextPrefOwnerId = next.authenticated
      ? (next.user?.user_id ?? null)
      : null;
    if (nextPrefOwnerId !== this.sidebarPrefOwnerId) {
      this.restoreSidebarPrefs(nextPrefOwnerId);
    }
    this.telemetry.updateSession(next);
    this.sidebar.applySession(next);
    this.packsModal?.setManageAllowed(next.capabilities.packs_manage_allowed);
    this.authModal.setSession(next);
    this.adminModal?.syncPublicPaused(next.quotas.public_renders_paused);
    this.syncGlobalStatusBanner(next);

    if (
      isBootstrap ||
      tenantChanged ||
      prev.capabilities.turnstile_required !== next.capabilities.turnstile_required ||
      prev.capabilities.turnstile_sitekey !== next.capabilities.turnstile_sitekey
    ) {
      void this.turnstile.syncSession(next);
    }

    if (next.auth_mode === "hosted") {
      const prevGridCols = this.state.grid_cols;
      const prevGridRows = this.state.grid_rows;
      const prevPdf = this.state.outputs.pdf;
      const clamped = clampFormStateToCapabilities(
        this.state,
        next.capabilities
      );
      if (clamped.changed) {
        this.sidebar.syncControlsFromState();
        const footprintChanged =
          prevGridCols !== this.state.grid_cols ||
          prevGridRows !== this.state.grid_rows ||
          prevPdf !== this.state.outputs.pdf ||
          clamped.resetItems.includes("atlas grid");
        if (!tenantChanged && (!isBootstrap || footprintChanged)) {
          this.requestFootprintImmediate({
            flyToResult: isBootstrap && this.initialFlyToResolved,
            syncMarker: isBootstrap,
            fitAfterLoad: false,
          });
        }
        if (!isBootstrap && !tenantChanged && clamped.resetItems.length > 0) {
          this.showToast(
            `Adjusted selections for your account tier (${clamped.resetItems.join(", ")})`,
            "info"
          );
        }
      }
    }

    if (next.role !== "admin" && this.adminModal?.opened) {
      this.adminModal.close();
    }
    if (!next.capabilities.packs_manage_allowed && this.packsModal?.opened) {
      this.packsModal.close();
    }

    if (isBootstrap) this.handleStartupAuth(next);

    if (tenantChanged) {
      this.requestFootprintImmediate({ flyToResult: false, syncMarker: true });
      void this.loadBasemapInfo();
      void this.loadPacks(false);
      void this.pollJobsOnce({ sessionChecked: true });
    }
  }

  private handleStartupAuth(session: SessionResponse): void {
    const token = this.pendingInviteToken;
    const authError = this.pendingAuthError;
    this.pendingInviteToken = this.pendingAuthError = null;

    if (session.auth_mode === "hosted" && token) {
      if (!session.authenticated) {
        this.authModal.open({
          mode: "invite",
          inviteToken: token,
          bannerError: authError,
        });
      }
    } else if (authError) {
      this.showToast(authError, "error");
      if (session.auth_mode === "hosted") {
        this.authModal.open({ mode: "signin", bannerError: authError });
      }
    }
  }

  private syncGlobalStatusBanner(session: SessionResponse): void {
    if (session.auth_mode !== "hosted") {
      this.globalStatusBannerEl.classList.add("hidden");
      this.globalStatusBannerEl.textContent = "";
      return;
    }

    const q = session.quotas;
    const globalExhausted =
      session.role === "guest" &&
      q.global_daily_public_limit !== null &&
      q.global_daily_public_used >= q.global_daily_public_limit;

    if (q.public_renders_paused && session.role === "admin") {
      this.globalStatusBannerEl.textContent =
        "Notice: Guest (public) renders are currently PAUSED (Friend and Admin renders remain active).";
      this.globalStatusBannerEl.classList.remove("hidden");
      return;
    }

    if (isGuestRenderPaused(session)) {
      this.globalStatusBannerEl.textContent =
        "Public renders are temporarily paused by the administrator.";
      this.globalStatusBannerEl.classList.remove("hidden");
      return;
    }

    if (globalExhausted) {
      this.globalStatusBannerEl.textContent =
        "Global daily public render budget reached. Sign in with an invited account or try again tomorrow.";
      this.globalStatusBannerEl.classList.remove("hidden");
      return;
    }

    this.globalStatusBannerEl.classList.add("hidden");
    this.globalStatusBannerEl.textContent = "";
  }

  private async handleSignOut(): Promise<void> {
    try {
      await this.changeSession(logoutSession);
      this.showToast("Signed out", "info");
    } catch (err) {
      const msg = this.handleApiError(err, "general", "Failed to sign out");
      this.showToast(msg, "error");
    }
  }

  private handleApiError(
    err: unknown,
    context: ApiErrorContext,
    fallbackMsg: string
  ): string {
    const info = classifyApiError(err);
    const msg = info.message || fallbackMsg;

    if (info.kind === "auth") {
      const wasAuthenticated = this.session.authenticated;
      void this.refreshSession({ force: true }).finally(() => {
        if (
          !this.session.authenticated &&
          (context !== "jobs-poll" || wasAuthenticated)
        ) {
          this.authModal.open({
            mode: "signin",
            bannerError: msg || "Please sign in to continue.",
          });
        }
      });
      return msg;
    }

    if (info.kind === "turnstile") {
      this.turnstile.reset();
      if (this.sidebarCollapsed) {
        this.setSidebarCollapsed(false, { persist: false });
      }
      return `Security verification failed or expired (${msg}). Please try again.`;
    }

    if (info.kind === "forbidden") {
      void this.refreshSession();
      if (
        this.session.auth_mode === "hosted" &&
        this.session.role === "guest" &&
        context !== "admin"
      ) {
        return `${msg} Sign in with an invited account for this feature.`;
      }
      return msg;
    }

    if (info.kind === "rate_limit" || info.kind === "service_unavailable") {
      void this.refreshSession();
      return msg;
    }

    return msg;
  }

  private async loadBasemapInfo(packId?: string | null): Promise<void> {
    const { signal } = this.workspaceRequests;
    try {
      const info = await fetchBasemapInfo(packId, signal);
      if (signal.aborted || (packId || "") !== this.state.pack_id) return;
      const existingBtn = this.basemapSwitcherEl.querySelector<HTMLButtonElement>(
        "[data-basemap='offline']"
      );

      if (!info.available || !info.format) {
        this.map.enableOfflineBasemap(info);
        if (existingBtn) existingBtn.remove();
        this.basemapSelectEl.querySelector("option[value='offline']")?.remove();
        this.pendingOfflineBasemapFromUrl = false;
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

      if (!this.basemapSelectEl.querySelector("option[value='offline']")) {
        const opt = document.createElement("option");
        opt.value = "offline";
        opt.textContent = "Offline";
        this.basemapSelectEl.appendChild(opt);
      }

      if (this.pendingOfflineBasemapFromUrl) {
        this.pendingOfflineBasemapFromUrl = false;
        this.selectBasemap("offline");
      }
    } catch (err) {
      console.debug("Offline basemap endpoint unavailable:", err);
    }
  }

  private getCurrentUrlViewState(): { basemap: BasemapId; zoom: number } {
    return {
      basemap: this.activeBasemap,
      zoom: Number(this.map.getZoom().toFixed(2)),
    };
  }

  private setShareLocation(shareId: string | null): void {
    this.activeShareId = shareId;
    const url = new URL(window.location.href);
    if (shareId) {
      url.searchParams.set("share", shareId);
    } else if (url.searchParams.has("share")) {
      url.searchParams.delete("share");
    } else {
      return;
    }
    if (url.href !== window.location.href) {
      window.history.replaceState(window.history.state, "", url);
    }
  }

  private workspaceEdited(): void {
    if (this.activeShareId) {
      this.setShareLocation(null);
      this.telemetry.setShortLinkResult(null);
    }
    if (this.telemetry.isShareMenuOpen()) {
      this.refreshSharePopoverSummary();
    }
  }

  private refreshSharePopoverSummary(): void {
    const originAndPath = `${window.location.origin}${window.location.pathname}`;
    const direct = buildDirectShareUrl(
      originAndPath,
      this.state,
      this.getCurrentUrlViewState()
    );
    const headline =
      this.state.title.trim() ||
      this.latestFootprint?.center_mgrs_10m ||
      this.state.center;
    const gridTag =
      this.state.grid_cols * this.state.grid_rows > 1
        ? ` · ${this.state.grid_cols}×${this.state.grid_rows}`
        : "";
    const parts: string[] = [
      `1:${this.state.scale.toLocaleString()}`,
      `${this.state.paper.toUpperCase()}${gridTag}`,
      this.state.color_mode,
    ];
    const counts: string[] = [];
    if (this.state.waypoints.length > 0) {
      counts.push(`${this.state.waypoints.length} WP`);
    }
    if (this.state.viewsheds.length > 0) {
      counts.push(`${this.state.viewsheds.length} LOS`);
    }
    if (this.state.rfLinks.length > 0) {
      counts.push(`${this.state.rfLinks.length} RF`);
    }
    if (this.state.tacticalGraphics.length > 0) {
      counts.push(`${this.state.tacticalGraphics.length} Tac`);
    }
    if (counts.length > 0) {
      parts.push(counts.join(", "));
    }

    this.telemetry.setShareSummary({
      headline,
      detail: parts.join(" · "),
      directTruncated: direct.truncated,
      hasLocalOverlayFile: Boolean(this.state.overlayFile),
      activeShareUrl: this.activeShareId
        ? `${originAndPath}?share=${encodeURIComponent(this.activeShareId)}`
        : null,
    });
  }

  private formatShareExpiryLabel(expiresAt: string | null): string {
    if (!expiresAt) return "Does not expire";
    const ms = Date.parse(expiresAt);
    if (Number.isNaN(ms)) return "Temporary link";
    const hours = Math.round((ms - Date.now()) / 3600000);
    if (hours >= 48) {
      return `Expires in ${Math.round(hours / 24)}d`;
    }
    if (hours >= 1) {
      return `Expires in ${hours}h`;
    }
    return "Expires soon";
  }

  private async handleCreateShortShareLink(): Promise<void> {
    const { signal } = this.workspaceRequests;
    this.telemetry.setShortLinkBusy(true);
    try {
      const snap = this.buildProjectSnapshot();
      const created = await createShareSnapshot(
        { name: snap.suggestedName, state: snap.state },
        signal
      );
      if (signal.aborted) return;
      const fullUrl = `${window.location.origin}${window.location.pathname}?share=${encodeURIComponent(created.share_id)}`;
      this.setShareLocation(created.share_id);
      this.telemetry.setShortLinkResult({
        url: fullUrl,
        expiresLabel: this.formatShareExpiryLabel(created.expires_at),
      });
      this.copyText(fullUrl, "Short share link copied to clipboard");
    } catch (err) {
      if (signal.aborted) return;
      const msg = this.handleApiError(
        err,
        "shares",
        "Failed to create share link"
      );
      this.showToast(msg, "error");
    } finally {
      if (!signal.aborted) this.telemetry.setShortLinkBusy(false);
    }
  }

  private async handleShareProject(project: SavedProject): Promise<void> {
    const { signal } = this.workspaceRequests;
    try {
      const created = await createShareSnapshot(
        { project_id: project.project_id },
        signal
      );
      if (signal.aborted) return;
      const fullUrl = `${window.location.origin}${window.location.pathname}?share=${encodeURIComponent(created.share_id)}`;
      this.copyText(fullUrl, `Copied share link for "${project.name}"`);
    } catch (err) {
      if (signal.aborted) return;
      const msg = this.handleApiError(
        err,
        "shares",
        "Failed to share project"
      );
      this.showToast(msg, "error");
    }
  }

  private handleCopyDirectLink(): void {
    const originAndPath = `${window.location.origin}${window.location.pathname}`;
    const direct = buildDirectShareUrl(
      originAndPath,
      this.state,
      this.getCurrentUrlViewState()
    );
    const toastMsg = direct.truncated
      ? "Direct link copied (compact URL omitted some markups; use Short Share Link for full fidelity)"
      : this.state.overlayFile
        ? "Direct link copied (local uploaded GPX/KML file is not embedded)"
        : "Direct link copied to clipboard";
    this.copyText(direct.url, toastMsg);
    this.telemetry.closeShareMenu();
  }

  private applyPlaceResult(place: GeocodeResult): void {
    this.clearStripPlanInternal(false);
    this.state.lat = place.lat;
    this.state.lon = place.lon;
    this.state.center = formatCoords(place.lat, place.lon);
    this.sidebar.setCenterInput(this.state.center, true);
    this.sidebar.setTitlePlaceholder(place.short_name.toUpperCase());
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    const view = this.getCurrentUrlViewState();

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
        dpi: this.state.dpi,
        grid_cols: this.state.grid_cols,
        grid_rows: this.state.grid_rows,
        dem_source: this.state.dem_source,
        pack_id: this.state.pack_id,
        basemap: view.basemap,
        map_zoom: view.zoom,
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

  private applySnapshotState(
    rawState: Record<string, unknown>,
    options: {
      toastLabel?: string;
      preserveShareId?: string | null;
      closeProjectsDrawer?: boolean;
      flyToCenter?: boolean;
    } = {}
  ): void {
    this.clearStripPlanInternal(false);
    this.handleRulerClear();
    this.setInteractionMode("pan");

    for (const ctrl of this.viewshedAborts.values()) ctrl.abort();
    this.viewshedAborts.clear();
    for (const ctrl of this.rfLinkAborts.values()) ctrl.abort();
    this.rfLinkAborts.clear();

    const view = hydrateStateFromSnapshot(rawState, this.state, {
      nextViewshedId: () => `vs-${this.viewshedSeq++}`,
      nextRFLinkId: () => `rf-${this.rfLinkSeq++}`,
    });

    let clampedResetItems: string[] = [];
    if (this.session.auth_mode === "hosted") {
      const clamped = clampFormStateToCapabilities(
        this.state,
        this.session.capabilities
      );
      clampedResetItems = clamped.resetItems;
    }

    if (view.basemap) {
      if (view.basemap === "offline") {
        const offlineBtn = this.basemapSwitcherEl.querySelector(
          "[data-basemap='offline']"
        );
        if (offlineBtn) {
          this.selectBasemap("offline");
        } else {
          this.pendingOfflineBasemapFromUrl = true;
        }
      } else {
        this.selectBasemap(view.basemap);
      }
    }

    this.sidebar.setFormError(null);
    this.sidebar.syncControlsFromState(true);

    if (options.preserveShareId) {
      this.activeShareId = options.preserveShareId;
    } else {
      this.workspaceEdited();
    }

    this.map.setCenter(this.state.lat, this.state.lon, {
      fly: options.flyToCenter ?? true,
      zoom: view.zoom ?? undefined,
    });
    this.map.updateWaypoints(this.state.waypoints);
    this.map.updateViewsheds(this.state.viewsheds);
    this.map.updateRFLinks(this.state.rfLinks, null);
    this.map.updateTacticalGraphics(this.state.tacticalGraphics, []);
    this.requestFootprintImmediate({
      flyToResult: false,
      syncMarker: true,
      fitAfterLoad:
        view.zoom === null && this.state.grid_cols * this.state.grid_rows > 1,
    });
    void this.loadBasemapInfo(this.state.pack_id || null);

    if (this.state.waypoints.length > 0) {
      this.scheduleRouteMetrics();
    } else {
      this.sidebar.setRouteMetrics(null);
    }

    if (options.closeProjectsDrawer) {
      this.setProjectsOpen(false);
    }

    if (options.toastLabel) {
      const tierNote =
        clampedResetItems.length > 0
          ? ` (adjusted ${clampedResetItems.join(", ")} for your account tier)`
          : "";
      this.showToast(`${options.toastLabel}${tierNote}`, "success");
    }

    if (this.state.viewsheds.length > 0 || this.state.rfLinks.length > 0) {
      this.sidebar.openViewshedSection();
      this.showToast(
        "Click Compute on any viewshed or RF link in the sidebar to run DEM analysis.",
        "info"
      );
    }
  }

  private loadProject(project: SavedProject): void {
    this.applySnapshotState(project.state as Record<string, unknown>, {
      toastLabel: `Loaded project "${project.name}"`,
      closeProjectsDrawer: true,
      flyToCenter: true,
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
      this.workspaceEdited();
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
      const msg = this.handleApiError(
        err,
        "strip",
        "Failed to plan strip atlas"
      );
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
    this.workspaceEdited();
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
    const workspaceSignal = this.workspaceRequests.signal;
    this.rulerAbort?.abort();
    const controller = new AbortController();
    this.rulerAbort = controller;
    this.sidebar.setRulerState(this.rulerPoints, this.rulerMetrics, true);

    let usedTurnstile = false;
    try {
      const packId = this.state.pack_id || null;
      const isHostedGuest =
        this.session.auth_mode === "hosted" && this.session.role === "guest";
      // Keep guest ruler measurements unlimited (pure geodesic) unless an offline pack is selected.
      const demSourceToSend =
        isHostedGuest && !packId ? null : this.state.dem_source;
      const needsTurnstile =
        this.turnstile.isActive() &&
        (demSourceToSend !== null || packId !== null);
      const turnstileToken = needsTurnstile
        ? await this.turnstile.acquireToken()
        : null;
      usedTurnstile = needsTurnstile && Boolean(turnstileToken);
      if (controller.signal.aborted) return;

      const res = await fetchRulerMetrics(
        this.rulerPoints,
        demSourceToSend,
        packId,
        controller.signal,
        turnstileToken
      );
      if (controller.signal.aborted) return;
      this.rulerMetrics = res;
      this.sidebar.setRulerState(this.rulerPoints, res, false);
    } catch (err) {
      if (controller.signal.aborted) return;
      this.sidebar.setRulerState(this.rulerPoints, this.rulerMetrics, false);
      const msg = this.handleApiError(
        err,
        "ruler",
        "Failed to compute ruler metrics"
      );
      this.showToast(msg, "error");
    } finally {
      if (usedTurnstile && !workspaceSignal.aborted) {
        this.turnstile.reset();
      }
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
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    this.workspaceEdited();
  }

  private handleTacticalClear(): void {
    this.sectorDraft = [];
    this.state.tacticalGraphics = [];
    this.sidebar.setSectorDraftCount(0);
    this.map.updateTacticalGraphics([], []);
    this.sidebar.renderTacticalGraphicsList();
    this.workspaceEdited();
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
    this.workspaceEdited();
    void this.computeRFLinkEntry(id);
  }

  private async computeRFLinkEntry(id: string): Promise<void> {
    const entry = this.state.rfLinks.find((e) => e.id === id);
    if (!entry) return;

    const workspaceSignal = this.workspaceRequests.signal;
    this.rfLinkAborts.get(id)?.abort();
    const controller = new AbortController();
    this.rfLinkAborts.set(id, controller);

    this.state.rfLinks = this.state.rfLinks.map((e) =>
      e.id === id ? { ...e, loading: true, error: null } : e
    );
    this.sidebar.renderRFLinksList();
    this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);

    let usedTurnstile = false;
    try {
      const needsTurnstile = this.turnstile.isActive();
      const turnstileToken = needsTurnstile
        ? await this.turnstile.acquireToken()
        : null;
      usedTurnstile = needsTurnstile && Boolean(turnstileToken);
      if (controller.signal.aborted) return;

      const profile = await fetchRFLinkProfile(
        entry.spec,
        this.state.dem_source,
        this.state.pack_id || null,
        controller.signal,
        turnstileToken
      );
      if (controller.signal.aborted) return;

      this.state.rfLinks = this.state.rfLinks.map((e) =>
        e.id === id ? { ...e, loading: false, error: null, profile } : e
      );
      this.sidebar.renderRFLinksList();
      this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);
      if (this.session.auth_mode === "hosted") {
        void this.refreshSession();
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg = this.handleApiError(err, "rf", "RF link profile failed");
      this.state.rfLinks = this.state.rfLinks.map((e) =>
        e.id === id ? { ...e, loading: false, error: msg } : e
      );
      this.sidebar.renderRFLinksList();
      this.map.updateRFLinks(this.state.rfLinks, this.rfPendingFrom);
      this.showToast(`RF link error: ${msg}`, "error");
    } finally {
      if (usedTurnstile && !workspaceSignal.aborted) {
        this.turnstile.reset();
      }
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
    this.workspaceEdited();
  }

  private handleRFLinksClear(): void {
    for (const ctrl of this.rfLinkAborts.values()) ctrl.abort();
    this.rfLinkAborts.clear();
    this.rfPendingFrom = null;
    this.state.rfLinks = [];
    this.state.include_rf_link_card = false;
    this.sidebar.syncControlsFromState();
    this.map.updateRFLinks([], null);
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    this.workspaceEdited();
    if (recompute) {
      void this.computeViewshedEntry(id);
    } else {
      this.map.updateViewsheds(this.state.viewsheds);
    }
  }

  private async computeViewshedEntry(id: string): Promise<void> {
    const entry = this.state.viewsheds.find((e) => e.id === id);
    if (!entry) return;

    const workspaceSignal = this.workspaceRequests.signal;
    this.viewshedAborts.get(id)?.abort();
    const controller = new AbortController();
    this.viewshedAborts.set(id, controller);

    this.state.viewsheds = this.state.viewsheds.map((e) =>
      e.id === id ? { ...e, loading: true, error: null } : e
    );
    this.sidebar.renderViewshedsList();

    let usedTurnstile = false;
    try {
      const needsTurnstile = this.turnstile.isActive();
      const turnstileToken = needsTurnstile
        ? await this.turnstile.acquireToken()
        : null;
      usedTurnstile = needsTurnstile && Boolean(turnstileToken);
      if (controller.signal.aborted) return;

      const preview = await fetchViewshedPreview(
        entry.observer,
        this.state.dem_source,
        this.state.pack_id || null,
        controller.signal,
        turnstileToken
      );
      if (controller.signal.aborted) return;

      this.state.viewsheds = this.state.viewsheds.map((e) =>
        e.id === id ? { ...e, loading: false, error: null, preview } : e
      );
      this.sidebar.renderViewshedsList();
      this.map.updateViewsheds(this.state.viewsheds);
      if (this.session.auth_mode === "hosted") {
        void this.refreshSession();
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg = this.handleApiError(
        err,
        "viewshed",
        "Viewshed computation failed"
      );
      this.state.viewsheds = this.state.viewsheds.map((e) =>
        e.id === id ? { ...e, loading: false, error: msg } : e
      );
      this.sidebar.renderViewshedsList();
      this.showToast(`Viewshed error: ${msg}`, "error");
    } finally {
      if (usedTurnstile && !workspaceSignal.aborted) {
        this.turnstile.reset();
      }
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
    this.workspaceEdited();
  }

  private handleViewshedsClear(): void {
    for (const ctrl of this.viewshedAborts.values()) ctrl.abort();
    this.viewshedAborts.clear();
    this.state.viewsheds = [];
    this.sidebar.renderViewshedsList();
    this.map.updateViewsheds([]);
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    this.workspaceEdited();
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
    const { signal } = this.workspaceRequests;
    try {
      const res = await fetchPacks(signal);
      if (signal.aborted) return;
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
      if (notify && !signal.aborted) {
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
      this.jobsPollTimer = null;
      void this.pollJobsOnce();
    }, delay);
  }

  private async pollJobsOnce(
    options: { sessionChecked?: boolean } = {}
  ): Promise<void> {
    if (document.hidden) {
      this.scheduleNextJobsPoll();
      return;
    }
    const { signal } = this.workspaceRequests;
    try {
      if (
        !options.sessionChecked &&
        (!this.sessionLoaded || this.session.auth_mode === "hosted")
      ) {
        const next = await this.refreshSession();
        if (!next) return;
      }
      if (signal.aborted) return;
      const jobs = await fetchJobs(signal);
      if (signal.aborted) return;
      this.jobs = jobs;
      this.drawer.setJobs(jobs);
      this.updateQueueBadge();
    } catch (err) {
      if (signal.aborted) return;
      if (err instanceof ApiError && (err.status === 401 || err.status === 503)) {
        this.handleApiError(err, "jobs-poll", "Failed to load jobs");
      }
    } finally {
      if (!signal.aborted) this.scheduleNextJobsPoll();
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
      dpi: this.state.dpi,
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
    const { signal } = this.workspaceRequests;
    this.sidebar.setSubmitting(true);
    this.sidebar.setFormError(null);
    let usedTurnstile = false;

    try {
      const needsTurnstile = this.turnstile.isActive();
      const turnstileToken = needsTurnstile
        ? await this.turnstile.acquireToken()
        : null;
      usedTurnstile = needsTurnstile && Boolean(turnstileToken);

      if (signal.aborted) return;
      const payload = this.buildJobPayload();
      const job = await submitJob(
        payload,
        this.state.overlayFile,
        turnstileToken,
        signal
      );
      if (signal.aborted) return;
      this.jobs = [job, ...this.jobs.filter((j) => j.job_id !== job.job_id)];
      this.drawer.setJobs(this.jobs);
      this.setDrawerOpen(true);
      this.showToast(
        `Queued sheet job #${job.job_id.slice(0, 8)}`,
        "success"
      );
      void this.loadHealth();
      if (this.session.auth_mode === "hosted") {
        void this.refreshSession();
      }
      this.scheduleNextJobsPoll();
    } catch (err) {
      if (signal.aborted) return;
      const msg = this.handleApiError(
        err,
        "job",
        "Failed to submit render job"
      );
      this.sidebar.setFormError(msg);
      this.showToast(msg, "error");
    } finally {
      if (!signal.aborted) {
        if (usedTurnstile) this.turnstile.reset();
        this.sidebar.setSubmitting(false);
      }
    }
  }

  private async runJobListMutation<T>(
    jobId: string,
    request: (signal: AbortSignal) => Promise<T>,
    applySuccess: (result: T) => { jobs: JobRecord[]; toast: string },
    fallbackError: string
  ): Promise<void> {
    const { signal } = this.workspaceRequests;
    try {
      const result = await request(signal);
      if (signal.aborted) return;
      const next = applySuccess(result);
      this.jobs = next.jobs;
      this.drawer.setJobs(this.jobs);
      this.updateQueueBadge();
      this.showToast(next.toast, "info");
    } catch (err) {
      if (signal.aborted) return;
      this.drawer.clearBusy(jobId);
      const msg = this.handleApiError(err, "general", fallbackError);
      this.showToast(msg, "error");
    }
  }

  private handleCancelJob(jobId: string): Promise<void> {
    return this.runJobListMutation(
      jobId,
      (signal) => cancelJob(jobId, signal),
      (updated) => ({
        jobs: this.jobs.map((j) => (j.job_id === jobId ? updated : j)),
        toast: `Cancelled job #${jobId.slice(0, 8)}`,
      }),
      "Failed to cancel job"
    );
  }

  private handleDeleteJob(jobId: string): Promise<void> {
    return this.runJobListMutation(
      jobId,
      (signal) => deleteJob(jobId, signal),
      () => ({
        jobs: this.jobs.filter((j) => j.job_id !== jobId),
        toast: `Deleted job #${jobId.slice(0, 8)}`,
      }),
      "Failed to delete job"
    );
  }

  private copyText(text: string, label: string): void {
    const { signal } = this.workspaceRequests;
    if (navigator.clipboard?.writeText) {
      navigator.clipboard
        .writeText(text)
        .then(() => {
          if (!signal.aborted) this.showToast(label, "success");
        })
        .catch(() => {
          if (!signal.aborted) this.showToast("Clipboard unavailable", "error");
        });
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
