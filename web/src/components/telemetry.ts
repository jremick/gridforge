import {
  escapeHtml,
  formatDeclinationCompass,
  formatMils,
  formatSignedDeg,
} from "../presets";
import type { FootprintResponse, HealthResponse } from "../types";

export interface TelemetryCallbacks {
  onToggleDrawer: () => void;
  onCopyMgrs: (mgrs: string) => void;
}

export class TelemetryBar {
  private readonly root: HTMLElement;
  private readonly mgrsEl: HTMLElement;
  private readonly utmEl: HTMLElement;
  private readonly sheetEl: HTMLElement;
  private readonly gmEl: HTMLElement;
  private readonly declEl: HTMLElement;
  private readonly statusDotEl: HTMLElement;
  private readonly statusTextEl: HTMLElement;
  private readonly queueToggleBtn: HTMLButtonElement;
  private readonly queueBadgeEl: HTMLElement;
  private latestMgrs = "";

  constructor(container: HTMLElement, callbacks: TelemetryCallbacks) {
    this.root = container;
    this.root.className = "gf-topbar";
    this.root.innerHTML = `
      <div class="gf-brand">
        <div class="gf-brand-mark" aria-hidden="true">
          <svg width="24" height="24" viewBox="0 0 32 32" fill="none">
            <rect width="32" height="32" rx="6" fill="#182028" stroke="#354556" stroke-width="1.5"/>
            <rect x="5" y="5" width="22" height="22" rx="2" stroke="#F59E0B" stroke-width="1.8" stroke-dasharray="4 2"/>
            <line x1="16" y1="5" x2="16" y2="27" stroke="#38BDF8" stroke-width="1.2" opacity="0.8"/>
            <line x1="5" y1="16" x2="27" y2="16" stroke="#38BDF8" stroke-width="1.2" opacity="0.8"/>
            <circle cx="16" cy="16" r="2.7" fill="#F59E0B"/>
          </svg>
        </div>
        <span class="gf-brand-name">GridForge</span>
        <span class="gf-brand-sub">Studio</span>
      </div>

      <div class="gf-telemetry-strip" role="status" aria-live="polite">
        <div class="gf-telemetry-item clickable" data-role="mgrs-box" title="Click to copy 10m MGRS coordinate">
          <span class="gf-telemetry-label">MGRS</span>
          <span class="gf-telemetry-value cyan" data-role="mgrs">—</span>
        </div>
        <div class="gf-telemetry-item">
          <span class="gf-telemetry-label">UTM</span>
          <span class="gf-telemetry-value" data-role="utm">—</span>
        </div>
        <div class="gf-telemetry-item">
          <span class="gf-telemetry-label">Sheet</span>
          <span class="gf-telemetry-value amber" data-role="sheet">—</span>
        </div>
        <div class="gf-telemetry-item">
          <span class="gf-telemetry-label">G-M</span>
          <span class="gf-telemetry-value" data-role="gm">—</span>
        </div>
        <div class="gf-telemetry-item">
          <span class="gf-telemetry-label">Decl</span>
          <span class="gf-telemetry-value" data-role="decl">—</span>
        </div>
      </div>

      <div class="gf-topbar-right">
        <button type="button" class="gf-status-pill" data-role="status-pill" title="Render worker status">
          <span class="gf-status-dot" data-role="status-dot"></span>
          <span class="mono" data-role="status-text">Connecting…</span>
        </button>
        <button type="button" class="gf-queue-toggle" data-role="queue-toggle" aria-expanded="false">
          <span>Queue</span>
          <span class="gf-queue-badge" data-role="queue-badge">0</span>
        </button>
      </div>
    `;

    this.mgrsEl = this.q("[data-role='mgrs']");
    this.utmEl = this.q("[data-role='utm']");
    this.sheetEl = this.q("[data-role='sheet']");
    this.gmEl = this.q("[data-role='gm']");
    this.declEl = this.q("[data-role='decl']");
    this.statusDotEl = this.q("[data-role='status-dot']");
    this.statusTextEl = this.q("[data-role='status-text']");
    this.queueToggleBtn = this.q<HTMLButtonElement>("[data-role='queue-toggle']");
    this.queueBadgeEl = this.q("[data-role='queue-badge']");

    const mgrsBox = this.q("[data-role='mgrs-box']");
    mgrsBox.addEventListener("click", () => {
      if (this.latestMgrs) {
        callbacks.onCopyMgrs(this.latestMgrs);
      }
    });

    this.q("[data-role='status-pill']").addEventListener("click", () => {
      callbacks.onToggleDrawer();
    });

    this.queueToggleBtn.addEventListener("click", () => {
      callbacks.onToggleDrawer();
    });
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Telemetry element not found: ${selector}`);
    return el;
  }

  updateFootprint(fp: FootprintResponse | null): void {
    if (!fp) {
      this.latestMgrs = "";
      this.mgrsEl.textContent = "—";
      this.utmEl.textContent = "—";
      this.sheetEl.textContent = "—";
      this.gmEl.textContent = "—";
      this.declEl.textContent = "—";
      return;
    }

    this.latestMgrs = fp.center_mgrs_10m;
    this.mgrsEl.textContent = fp.center_mgrs_10m;
    this.utmEl.textContent = `${fp.utm_zone}${fp.utm_hemisphere} (EPSG:${fp.utm_epsg})`;
    this.sheetEl.textContent = `${fp.ground_width_km.toFixed(2)} × ${fp.ground_height_km.toFixed(2)} km`;

    const d = fp.declination;
    this.gmEl.textContent = `${formatSignedDeg(d.gm_angle_deg)} (${formatMils(d.gm_angle_mils)})`;
    this.declEl.textContent = formatDeclinationCompass(d.magnetic_declination_deg);
    this.declEl.title = `${d.model_name} (${d.decimal_year.toFixed(2)}) · Annual change: ${formatSignedDeg(d.annual_change_deg)}/yr · Grid conv: ${formatSignedDeg(d.grid_convergence_deg)}`;
  }

  updateHealth(health: HealthResponse | null, errorMsg?: string): void {
    this.statusDotEl.className = "gf-status-dot";
    if (!health) {
      this.statusDotEl.classList.add("offline");
      this.statusTextEl.textContent = errorMsg ? "API Offline" : "Offline";
      return;
    }

    const statusPill = this.q("[data-role='status-pill']");
    if (health.worker_alive) {
      this.statusDotEl.classList.add(health.queue_depth > 0 ? "busy" : "online");
      this.statusTextEl.textContent =
        health.queue_depth > 0
          ? `Online · Queue: ${health.queue_depth}`
          : `Online · Queue: 0`;
      statusPill.title = `Worker alive · RAM budget: ${health.memory_budget_mb} MB · Offline packs: ${health.packs_count}`;
    } else {
      this.statusDotEl.classList.add("offline");
      this.statusTextEl.textContent = `Worker Offline · Queue: ${health.queue_depth}`;
      statusPill.title = health.worker_last_seen
        ? `Last seen: ${escapeHtml(health.worker_last_seen)}`
        : "Worker has not checked in yet";
    }
  }

  updateQueueCounts(activeCount: number, totalCount: number, drawerOpen: boolean): void {
    this.queueToggleBtn.classList.toggle("active", drawerOpen);
    this.queueToggleBtn.setAttribute("aria-expanded", String(drawerOpen));
    this.queueBadgeEl.textContent = String(activeCount > 0 ? activeCount : totalCount);
    this.queueBadgeEl.classList.toggle("has-active", activeCount > 0);
  }
}
