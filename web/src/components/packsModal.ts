import {
  deletePack,
  fetchPackRegions,
  fetchPackTasks,
  fetchPacks,
  startPackBuild,
} from "../api";
import { escapeHtml, formatBytes, formatDuration } from "../presets";
import type {
  PackBuildTask,
  PackDemTier,
  PackRegionPreset,
  PackSummary,
} from "../types";

export interface PacksModalCallbacks {
  onPacksUpdated: (packs: PackSummary[]) => void;
  onSelectPack: (packId: string) => void;
  onToast: (message: string, kind: "success" | "error" | "info") => void;
  getCurrentBBoxString: () => string | null;
}

type RegionTab = "tactical" | "state" | "custom";

export class PacksModal {
  private readonly root: HTMLElement;
  private readonly callbacks: PacksModalCallbacks;
  private readonly installedEl: HTMLElement;
  private readonly regionsEl: HTMLElement;
  private readonly tasksEl: HTMLElement;
  private readonly demTierSelect: HTMLSelectElement;
  private readonly incOsmCheck: HTMLInputElement;
  private readonly incDemCheck: HTMLInputElement;
  private readonly incOverlaysCheck: HTMLInputElement;

  private isOpen = false;
  private activeTab: RegionTab = "tactical";
  private packs: PackSummary[] = [];
  private regions: PackRegionPreset[] = [];
  private tasks: PackBuildTask[] = [];
  private deletingIds = new Set<string>();
  private submittingRegion: string | null = null;
  private pollTimer: number | null = null;
  private customBboxVal = "";
  private customNameVal = "";

  constructor(container: HTMLElement, callbacks: PacksModalCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "gf-modal-backdrop hidden";
    this.root.setAttribute("role", "dialog");
    this.root.setAttribute("aria-modal", "true");
    this.root.setAttribute("aria-label", "Offline Pack Manager");

    this.root.innerHTML = `
      <div class="gf-modal-panel" data-role="modal-panel">
        <div class="gf-modal-header">
          <div class="gf-modal-title-group">
            <span class="gf-section-index">AIR-GAP</span>
            <h2 class="gf-modal-title">Offline Pack Manager</h2>
          </div>
          <div class="gf-drawer-actions">
            <button type="button" class="gf-icon-btn" data-role="modal-refresh" title="Refresh packs &amp; tasks">↻</button>
            <button type="button" class="gf-icon-btn" data-role="modal-close" title="Close (Esc)">×</button>
          </div>
        </div>

        <div class="gf-modal-body">
          <div class="gf-pack-tasks hidden" data-role="pack-tasks"></div>

          <section class="gf-modal-section">
            <div class="gf-modal-section-head">
              <h3>Installed Offline Packs</h3>
              <span class="gf-sublabel">Air-gapped DEM + OSM PBF + Overlays</span>
            </div>
            <div class="gf-pack-installed-list" data-role="installed-list"></div>
          </section>

          <section class="gf-modal-section">
            <div class="gf-modal-section-head">
              <h3>Build Australia Regional Pack</h3>
              <div class="gf-segmented gf-modal-tabs" data-role="region-tabs">
                <button type="button" class="gf-seg-btn active" data-tab="tactical">Tactical (7)</button>
                <button type="button" class="gf-seg-btn" data-tab="state">States (8)</button>
                <button type="button" class="gf-seg-btn" data-tab="custom">Custom BBox</button>
              </div>
            </div>

            <div class="gf-pack-build-opts">
              <label class="gf-inline-check">
                <span>DEM</span>
                <select class="gf-mini-select" data-role="pack-dem-tier">
                  <option value="srtm-1s">DEA SRTM 1s (AHD)</option>
                  <option value="au-elvis">GA 5m / SRTM (AHD)</option>
                  <option value="glo30">Copernicus GLO-30</option>
                </select>
              </label>
              <label class="gf-inline-check">
                <input type="checkbox" data-role="pack-inc-osm" checked />
                <span>OSM</span>
              </label>
              <label class="gf-inline-check">
                <input type="checkbox" data-role="pack-inc-dem" checked />
                <span>DEM</span>
              </label>
              <label class="gf-inline-check">
                <input type="checkbox" data-role="pack-inc-overlays" checked />
                <span>AU Overlays</span>
              </label>
            </div>

            <div class="gf-pack-regions-grid" data-role="regions-list"></div>
          </section>
        </div>
      </div>
    `;

    this.installedEl = this.q("[data-role='installed-list']");
    this.regionsEl = this.q("[data-role='regions-list']");
    this.tasksEl = this.q("[data-role='pack-tasks']");
    this.demTierSelect = this.q<HTMLSelectElement>("[data-role='pack-dem-tier']");
    this.incOsmCheck = this.q<HTMLInputElement>("[data-role='pack-inc-osm']");
    this.incDemCheck = this.q<HTMLInputElement>("[data-role='pack-inc-dem']");
    this.incOverlaysCheck = this.q<HTMLInputElement>(
      "[data-role='pack-inc-overlays']"
    );

    this.bindEvents();
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`PacksModal element not found: ${selector}`);
    return el;
  }

  private bindEvents(): void {
    this.root.addEventListener("click", (e) => {
      if (e.target === this.root) {
        this.close();
      }
    });

    this.q("[data-role='modal-close']").addEventListener("click", () => {
      this.close();
    });

    this.q("[data-role='modal-refresh']").addEventListener("click", () => {
      void this.refreshAll();
    });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-tab]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const tab = btn.dataset.tab as RegionTab;
          this.activeTab = tab;
          this.root
            .querySelectorAll<HTMLButtonElement>("[data-tab]")
            .forEach((b) => b.classList.toggle("active", b === btn));
          this.renderRegions();
        });
      });

    this.installedEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-pack-action]");
      if (!btn) return;
      const action = btn.dataset.packAction;
      const packId = btn.dataset.packId;
      if (!packId) return;
      if (action === "use") {
        this.callbacks.onSelectPack(packId);
        this.close();
      } else if (action === "delete") {
        void this.handleDeletePack(packId);
      }
    });

    this.regionsEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-region-action]");
      if (!btn) return;
      const action = btn.dataset.regionAction;
      if (action === "build") {
        const regionId = btn.dataset.regionId;
        if (regionId) void this.handleBuildPack(regionId);
      } else if (action === "fill-sheet-bbox") {
        const bbox = this.callbacks.getCurrentBBoxString();
        if (bbox) {
          this.customBboxVal = bbox;
          this.renderRegions();
        } else {
          this.callbacks.onToast("No active sheet bounds yet", "info");
        }
      } else if (action === "build-custom") {
        const bbox = this.customBboxVal.trim();
        if (!bbox) {
          this.callbacks.onToast(
            "Enter a WGS84 bounding box (west,south,east,north)",
            "error"
          );
          return;
        }
        void this.handleBuildPack(bbox, this.customNameVal.trim() || null);
      }
    });

    this.regionsEl.addEventListener("input", (e) => {
      const target = e.target as HTMLInputElement | null;
      if (!target) return;
      if (target.dataset.customField === "bbox") {
        this.customBboxVal = target.value;
      } else if (target.dataset.customField === "name") {
        this.customNameVal = target.value;
      }
    });
  }

  get opened(): boolean {
    return this.isOpen;
  }

  open(): void {
    this.isOpen = true;
    this.root.classList.remove("hidden");
    void this.refreshAll();
  }

  close(): void {
    this.isOpen = false;
    this.root.classList.add("hidden");
    this.stopPolling();
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) {
      window.clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private schedulePollIfActive(): void {
    this.stopPolling();
    if (!this.isOpen) return;
    const hasActive = this.tasks.some(
      (t) => t.status === "queued" || t.status === "running"
    );
    if (hasActive) {
      this.pollTimer = window.setTimeout(() => {
        void this.refreshTasksAndPacks();
      }, 2000);
    }
  }

  private async refreshAll(): Promise<void> {
    const [packsRes, regionsRes, tasksRes] = await Promise.allSettled([
      fetchPacks(),
      this.regions.length > 0 ? Promise.resolve(this.regions) : fetchPackRegions(),
      fetchPackTasks(),
    ]);

    if (packsRes.status === "fulfilled") {
      this.packs = packsRes.value.packs;
      this.callbacks.onPacksUpdated(this.packs);
    }
    if (regionsRes.status === "fulfilled") {
      this.regions = regionsRes.value;
    }
    if (tasksRes.status === "fulfilled") {
      this.tasks = tasksRes.value;
    }

    this.renderInstalled();
    this.renderTasks();
    this.renderRegions();
    this.schedulePollIfActive();
  }

  private async refreshTasksAndPacks(): Promise<void> {
    const prevRunningIds = new Set(
      this.tasks
        .filter((t) => t.status === "queued" || t.status === "running")
        .map((t) => t.task_id)
    );

    try {
      const tasks = await fetchPackTasks();
      this.tasks = tasks;
      this.renderTasks();

      const justFinished = tasks.some(
        (t) =>
          prevRunningIds.has(t.task_id) &&
          (t.status === "succeeded" || t.status === "failed")
      );
      if (justFinished) {
        const packsRes = await fetchPacks();
        this.packs = packsRes.packs;
        this.callbacks.onPacksUpdated(this.packs);
        this.renderInstalled();
        const succeeded = tasks.find(
          (t) => prevRunningIds.has(t.task_id) && t.status === "succeeded"
        );
        if (succeeded) {
          this.callbacks.onToast(
            `Offline pack ready: ${succeeded.manifest?.name ?? succeeded.region}`,
            "success"
          );
        }
      }
    } catch {
      // Silently wait for next tick if API briefly busy
    } finally {
      this.schedulePollIfActive();
    }
  }

  private async handleBuildPack(
    regionOrBbox: string,
    customName?: string | null
  ): Promise<void> {
    this.submittingRegion = regionOrBbox;
    this.renderRegions();

    try {
      const task = await startPackBuild({
        region_or_bbox: regionOrBbox,
        name: customName ?? null,
        include_osm: this.incOsmCheck.checked,
        include_dem: this.incDemCheck.checked,
        include_overlays: this.incOverlaysCheck.checked,
        dem_tier: this.demTierSelect.value as PackDemTier,
      });
      this.tasks = [task, ...this.tasks.filter((t) => t.task_id !== task.task_id)];
      this.renderTasks();
      this.callbacks.onToast(`Started pack build: ${task.region}`, "info");
      this.schedulePollIfActive();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to start pack build";
      this.callbacks.onToast(msg, "error");
    } finally {
      this.submittingRegion = null;
      this.renderRegions();
    }
  }

  private async handleDeletePack(packId: string): Promise<void> {
    this.deletingIds.add(packId);
    this.renderInstalled();

    try {
      await deletePack(packId);
      this.packs = this.packs.filter((p) => p.pack_id !== packId);
      this.callbacks.onPacksUpdated(this.packs);
      this.callbacks.onToast(`Deleted offline pack "${packId}"`, "info");
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to delete offline pack";
      this.callbacks.onToast(msg, "error");
    } finally {
      this.deletingIds.delete(packId);
      this.renderInstalled();
    }
  }

  private renderTasks(): void {
    const visible = this.tasks.slice(0, 3);
    if (visible.length === 0) {
      this.tasksEl.classList.add("hidden");
      this.tasksEl.innerHTML = "";
      return;
    }

    this.tasksEl.classList.remove("hidden");
    this.tasksEl.innerHTML = visible
      .map((t) => {
        const statusText = t.message || t.stage || t.status;
        const elapsed =
          typeof t.elapsed_seconds === "number"
            ? ` · ${formatDuration(t.elapsed_seconds)}`
            : "";
        return `
          <div class="gf-pack-task-row status-${escapeHtml(t.status)}">
            <div class="gf-pack-task-info">
              <span class="gf-pack-task-name">${escapeHtml(t.region)}</span>
              <span class="gf-pack-task-msg mono">${escapeHtml(statusText)}${escapeHtml(elapsed)}</span>
              ${t.error ? `<span class="gf-pack-task-err mono">${escapeHtml(t.error)}</span>` : ""}
            </div>
            <span class="gf-badge ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span>
          </div>
        `;
      })
      .join("");
  }

  private renderInstalled(): void {
    if (this.packs.length === 0) {
      this.installedEl.innerHTML = `
        <div class="gf-wp-empty">
          No offline packs installed yet. Build any curated region below for 100% air-gapped rendering.
        </div>
      `;
      return;
    }

    this.installedEl.innerHTML = this.packs
      .map((p) => {
        const busy = this.deletingIds.has(p.pack_id);
        const zones = p.utm_zones.join(", ");
        return `
          <div class="gf-pack-card">
            <div class="gf-pack-card-main">
              <div class="gf-pack-card-title">${escapeHtml(p.name)}</div>
              <div class="gf-pack-card-meta mono">
                ${escapeHtml(p.region)} · UTM ${escapeHtml(zones || "—")} · ${escapeHtml(p.vertical_datum)} · ${escapeHtml(formatBytes(p.size_bytes))}
              </div>
            </div>
            <div class="gf-pack-card-actions">
              <button type="button" class="gf-mini-btn" data-pack-action="use" data-pack-id="${escapeHtml(p.pack_id)}">Use</button>
              <button type="button" class="gf-mini-btn danger" data-pack-action="delete" data-pack-id="${escapeHtml(p.pack_id)}" ${busy ? "disabled" : ""}>${busy ? "…" : "Delete"}</button>
            </div>
          </div>
        `;
      })
      .join("");
  }

  private renderRegions(): void {
    const anyRunning = this.tasks.some(
      (t) => t.status === "queued" || t.status === "running"
    );

    if (this.activeTab === "custom") {
      this.regionsEl.className = "gf-pack-custom-form";
      this.regionsEl.innerHTML = `
        <div class="gf-field-row">
          <div class="gf-field">
            <label class="gf-label">Pack Name (optional)</label>
            <input
              type="text"
              class="gf-input"
              data-custom-field="name"
              value="${escapeHtml(this.customNameVal)}"
              placeholder="e.g. Snowy High Country"
            />
          </div>
          <div class="gf-field">
            <label class="gf-label">
              <span>WGS84 BBox (w,s,e,n)</span>
              <button type="button" class="gf-mini-btn" data-region-action="fill-sheet-bbox">Use Current Sheet</button>
            </label>
            <input
              type="text"
              class="gf-input mono"
              data-custom-field="bbox"
              value="${escapeHtml(this.customBboxVal)}"
              placeholder="150.10,-33.90,150.55,-33.55"
            />
          </div>
        </div>
        <button
          type="button"
          class="gf-strip-btn"
          data-region-action="build-custom"
          ${anyRunning || this.submittingRegion !== null ? "disabled" : ""}
        >
          <span>${anyRunning ? "Pack Build in Progress…" : "Build Custom BBox Pack"}</span>
        </button>
      `;
      return;
    }

    this.regionsEl.className = "gf-pack-regions-grid";
    const filtered = this.regions.filter((r) => r.kind === this.activeTab);
    if (filtered.length === 0) {
      this.regionsEl.innerHTML = `<div class="gf-wp-empty">Loading curated Australian regions…</div>`;
      return;
    }

    this.regionsEl.innerHTML = filtered
      .map((r) => {
        const b = r.bbox_wgs84;
        const bboxText = `${b.min_x.toFixed(1)}°E–${b.max_x.toFixed(1)}°E, ${Math.abs(b.max_y).toFixed(1)}°S–${Math.abs(b.min_y).toFixed(1)}°S`;
        const busy = this.submittingRegion === r.region_id;
        return `
          <div class="gf-region-card">
            <div class="gf-region-info">
              <div class="gf-region-name">${escapeHtml(r.name)}</div>
              <div class="gf-region-meta mono">UTM ${escapeHtml(r.utm_zones.join(", "))} · ${escapeHtml(bboxText)}</div>
            </div>
            <button
              type="button"
              class="gf-mini-btn gf-build-btn"
              data-region-action="build"
              data-region-id="${escapeHtml(r.region_id)}"
              ${anyRunning || busy ? "disabled" : ""}
            >${busy ? "Starting…" : "Build Pack"}</button>
          </div>
        `;
      })
      .join("");
  }
}
