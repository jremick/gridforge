import "./styles.css";
import {
  cancelJob,
  deleteJob,
  fetchFootprint,
  fetchHealth,
  fetchJobs,
  fetchPacks,
  submitJob,
} from "./api";
import { JobsDrawer } from "./components/jobsDrawer";
import { SidebarPanel } from "./components/sidebar";
import { TelemetryBar } from "./components/telemetry";
import { GridForgeMap } from "./map";
import {
  BASEMAP_OPTIONS,
  createInitialFormState,
  escapeHtml,
  formatCoords,
} from "./presets";
import type {
  BasemapId,
  CreateJobPayload,
  FootprintResponse,
  JobRecord,
  LocationPreset,
} from "./types";

const FOOTPRINT_DEBOUNCE_MS = 180;
const ACTIVE_JOBS_POLL_MS = 2000;
const IDLE_JOBS_POLL_MS = 10000;
const HEALTH_POLL_MS = 5000;

class GridForgeApp {
  private readonly state = createInitialFormState();
  private readonly shellEl: HTMLElement;
  private readonly telemetry: TelemetryBar;
  private readonly sidebar: SidebarPanel;
  private readonly drawer: JobsDrawer;
  private readonly map: GridForgeMap;
  private readonly footprintBannerEl: HTMLElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly collapseBtnEl: HTMLButtonElement;

  private sidebarCollapsed = false;
  private drawerOpen = false;
  private activeBasemap: BasemapId = "opentopo";
  private latestFootprint: FootprintResponse | null = null;
  private jobs: JobRecord[] = [];

  private footprintTimer: number | null = null;
  private footprintAbort: AbortController | null = null;
  private jobsPollTimer: number | null = null;

  constructor(root: HTMLElement) {
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
              <button type="button" class="gf-map-icon-btn" data-map-action="fit" title="Fit sheet footprint">⌖ Fit</button>
              <button type="button" class="gf-map-icon-btn" data-map-action="zoom-in" title="Zoom in">+</button>
              <button type="button" class="gf-map-icon-btn" data-map-action="zoom-out" title="Zoom out">−</button>
              <button type="button" class="gf-map-icon-btn" data-map-action="north" title="Reset north">N</button>
            </div>
          </div>

          <div class="gf-footprint-banner hidden" data-role="footprint-banner"></div>

          <div class="gf-map-hint">
            <span>Click map or drag reticle to move sheet</span>
            <span class="mono" data-role="cursor-coords">—</span>
          </div>

          <aside data-role="drawer"></aside>
        </main>
        <div class="gf-toast-container" data-role="toasts"></div>
      </div>
    `;

    this.shellEl = this.q(root, "[data-role='shell']");
    this.footprintBannerEl = this.q(root, "[data-role='footprint-banner']");
    this.cursorCoordsEl = this.q(root, "[data-role='cursor-coords']");
    this.toastContainerEl = this.q(root, "[data-role='toasts']");
    this.collapseBtnEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='sidebar-toggle']"
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
          this.state.center = centerText;
          this.requestFootprintImmediate({ flyToResult: true, syncMarker: true });
        },
        onPresetSelect: (preset) => this.applyPreset(preset),
        onFootprintParamsChange: () => this.scheduleFootprint(),
        onStateChange: () => {
          if (this.latestFootprint) {
            this.map.updateFootprint(
              this.latestFootprint.footprint_geojson,
              this.latestFootprint.grid_geojson,
              this.state.carto.show_grid
            );
          }
        },
        onRefreshPacks: () => void this.loadPacks(true),
        onSubmitJob: () => void this.handleSubmitJob(),
      }
    );

    this.drawer = new JobsDrawer(this.q(root, "[data-role='drawer']"), {
      onClose: () => this.setDrawerOpen(false),
      onRefresh: () => void this.pollJobsOnce(),
      onCancelJob: (jobId) => void this.handleCancelJob(jobId),
      onDeleteJob: (jobId) => void this.handleDeleteJob(jobId),
    });

    this.map = new GridForgeMap(this.q(root, "[data-role='map']"), {
      onCenterDrag: (lat, lon) => {
        this.state.lat = lat;
        this.state.lon = lon;
        this.state.center = formatCoords(lat, lon);
        this.sidebar.setCenterInput(this.state.center, true);
        this.scheduleFootprint();
      },
      onCenterCommit: (lat, lon) => {
        this.state.lat = lat;
        this.state.lon = lon;
        this.state.center = formatCoords(lat, lon);
        this.sidebar.setCenterInput(this.state.center, true);
        this.scheduleFootprint();
      },
      onCursorMove: (lat, lon) => {
        this.cursorCoordsEl.textContent = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
      },
    });

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
          if (action === "fit") {
            this.map.fitFootprint(this.sidebarCollapsed ? 24 : 368);
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
      const tag = (e.target as HTMLElement | null)?.tagName;
      const inInput =
        tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (e.key === "/" && !inInput) {
        e.preventDefault();
        if (this.sidebarCollapsed) {
          this.collapseBtnEl.click();
        }
        this.sidebar.focusCenterInput();
      } else if (e.key === "Escape" && this.drawerOpen) {
        this.setDrawerOpen(false);
      }
    });
  }

  private bootstrap(): void {
    this.requestFootprintImmediate({ flyToResult: false, syncMarker: true });
    void this.loadHealth();
    void this.loadPacks(false);
    void this.pollJobsOnce();

    window.setInterval(() => void this.loadHealth(), HEALTH_POLL_MS);
    window.setInterval(() => this.drawer.tickElapsed(), 1000);
  }

  private applyPreset(preset: LocationPreset): void {
    this.state.lat = preset.lat;
    this.state.lon = preset.lon;
    this.state.center = formatCoords(preset.lat, preset.lon);
    this.state.scale = preset.scale;
    this.state.paper = preset.paper;
    this.state.units = preset.units;
    this.sidebar.setTitlePlaceholder(preset.defaultTitle);
    this.sidebar.syncControlsFromState();
    this.map.setCenter(preset.lat, preset.lon, { fly: true, zoom: preset.zoom });
    this.requestFootprintImmediate({ flyToResult: false, syncMarker: false });
  }

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
      void this.loadFootprint({ flyToResult: false, syncMarker: false });
    }, FOOTPRINT_DEBOUNCE_MS);
  }

  private requestFootprintImmediate(options: {
    flyToResult: boolean;
    syncMarker: boolean;
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
    return {
      center: this.state.center.trim() || formatCoords(this.state.lat, this.state.lon),
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
    };
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
        .then(() => this.showToast(label, "info"))
        .catch(() => this.showToast("Clipboard unavailable", "error"));
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
