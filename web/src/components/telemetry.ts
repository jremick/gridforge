import {
  escapeHtml,
  formatDeclinationCompass,
  formatSignedDeg,
  formatSignedMilsNumber,
} from "../presets";
import { formatAccountPill } from "../session";
import type {
  FootprintResponse,
  HealthResponse,
  SessionResponse,
  StripPlanResponse,
} from "../types";

export interface SharePopoverSummary {
  headline: string;
  detail: string;
  directTruncated: boolean;
  hasLocalOverlayFile: boolean;
  activeShareUrl?: string | null;
}

export interface ShortLinkDisplay {
  url: string;
  expiresLabel: string;
}

export interface TelemetryCallbacks {
  onToggleSidebar?: () => void;
  onToggleDrawer: () => void;
  onToggleProjects: () => void;
  onCopyMgrs: (mgrs: string) => void;
  onShareOpen?: () => void;
  onCopyDirectLink?: () => void;
  onCreateShortLink?: () => void;
  onCopyShareUrl?: (url: string) => void;
  onOpenAuth?: () => void;
  onSignOut?: () => void;
  onOpenAdmin?: () => void;
}

const ICON_SIDEBAR = `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="1.5" y="2.25" width="13" height="11.5" rx="2" stroke="currentColor" stroke-width="1.4"/><line x1="6" y1="2.5" x2="6" y2="13.5" stroke="currentColor" stroke-width="1.4"/></svg>`;
const ICON_SHARE = `<svg width="13.5" height="13.5" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M6.75 9.25L9.25 6.75" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M8.8 10.6L7.4 12C6.3 13.1 4.5 13.1 3.4 12C2.3 10.9 2.3 9.1 3.4 8L4.8 6.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M7.2 5.4L8.6 4C9.7 2.9 11.5 2.9 12.6 4C13.7 5.1 13.7 6.9 12.6 8L11.2 9.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`;
const ICON_PROJECTS = `<svg width="13.5" height="13.5" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2 4.25C2 3.42 2.67 2.75 3.5 2.75H6.2L7.55 4.25H12.5C13.33 4.25 14 4.92 14 5.75V11.75C14 12.58 13.33 13.25 12.5 13.25H3.5C2.67 13.25 2 12.58 2 11.75V4.25Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
const ICON_ADMIN = `<svg width="13.5" height="13.5" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2.5 4.5H13.5M2.5 8H13.5M2.5 11.5H13.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="5.5" cy="4.5" r="1.4" fill="#0c1116" stroke="currentColor" stroke-width="1.3"/><circle cx="10.5" cy="8" r="1.4" fill="#0c1116" stroke="currentColor" stroke-width="1.3"/><circle cx="6.5" cy="11.5" r="1.4" fill="#0c1116" stroke="currentColor" stroke-width="1.3"/></svg>`;
const ICON_CHEVRON = `<svg class="gf-menu-chevron" width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICON_COPY = `<svg class="gf-copy-glyph" width="11.5" height="11.5" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" stroke-width="1.4"/><path d="M10.5 5.5V3.75C10.5 3.06 9.94 2.5 9.25 2.5H3.75C3.06 2.5 2.5 3.06 2.5 3.75V9.25C2.5 9.94 3.06 10.5 3.75 10.5H5.5" stroke="currentColor" stroke-width="1.4"/></svg>`;

export class TelemetryBar {
  private readonly root: HTMLElement;
  private readonly callbacks: TelemetryCallbacks;
  private readonly sidebarToggleBtn: HTMLButtonElement;
  private readonly brandEl: HTMLElement;
  private readonly brandNameEl: HTMLElement;
  private readonly brandSubEl: HTMLElement;
  private readonly topbarRightEl: HTMLElement;
  private readonly mgrsBoxBtn: HTMLButtonElement;
  private readonly mgrsEl: HTMLElement;
  private readonly utmEl: HTMLElement;
  private readonly sheetEl: HTMLElement;
  private readonly gmEl: HTMLElement;
  private readonly declEl: HTMLElement;
  private readonly statusDotEl: HTMLElement;
  private readonly queueLabelEl: HTMLElement;
  private readonly shareWrapEl: HTMLElement;
  private readonly shareToggleBtn: HTMLButtonElement;
  private readonly sharePopoverEl: HTMLElement;
  private readonly shareHeadlineEl: HTMLElement;
  private readonly shareDetailEl: HTMLElement;
  private readonly shareNoteEl: HTMLElement;
  private readonly shareCopyDirectBtn: HTMLButtonElement;
  private readonly shareCreateShortBtn: HTMLButtonElement;
  private readonly shareShortResultEl: HTMLElement;
  private readonly shareShortExpiryEl: HTMLElement;
  private readonly shareShortInput: HTMLInputElement;
  private readonly shareShortCopyBtn: HTMLButtonElement;
  private readonly projectsToggleBtn: HTMLButtonElement;
  private readonly queueToggleBtn: HTMLButtonElement;
  private readonly queueBadgeEl: HTMLElement;
  private authDividerEl: HTMLElement | null = null;
  private authControlsEl: HTMLElement | null = null;
  private accountMenuOpen = false;
  private shareMenuOpen = false;
  private shortLinkBusy = false;
  private latestMgrs = "";
  private workerStatusLabel = "Connecting";
  private workerOffline = false;
  private latestQueueCount = 0;

  private readonly handleDocClick = (e: Event): void => {
    const target = e.target as Node | null;
    if (
      this.accountMenuOpen &&
      this.authControlsEl &&
      target &&
      !this.authControlsEl.contains(target)
    ) {
      this.setAccountMenuOpen(false, { restoreFocus: false });
    }
    if (this.shareMenuOpen && target && !this.shareWrapEl.contains(target)) {
      this.setShareMenuOpen(false, { restoreFocus: false });
    }
  };

  private readonly handleDocKeyDown = (e: KeyboardEvent): void => {
    if (e.key !== "Escape") return;
    if (this.shareMenuOpen) {
      e.preventDefault();
      e.stopPropagation();
      this.setShareMenuOpen(false, { restoreFocus: true });
      return;
    }
    if (this.accountMenuOpen) {
      e.preventDefault();
      e.stopPropagation();
      this.setAccountMenuOpen(false, { restoreFocus: true });
    }
  };

  constructor(container: HTMLElement, callbacks: TelemetryCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "gf-topbar";
    this.root.innerHTML = `
      <div class="gf-topbar-left">
        <button
          type="button"
          class="gf-sidebar-header-btn active"
          data-role="sidebar-toggle"
          aria-expanded="true"
          aria-controls="gf-studio-sidebar"
          aria-label="Hide studio sidebar"
          title="Hide studio sidebar ([)"
        >${ICON_SIDEBAR}</button>

        <div class="gf-brand" data-role="brand">
          <div class="gf-brand-mark" aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 32 32" fill="none">
              <rect width="32" height="32" rx="6" fill="#182028" stroke="#354556" stroke-width="1.5"/>
              <rect x="5" y="5" width="22" height="22" rx="2" stroke="#F59E0B" stroke-width="1.8" stroke-dasharray="4 2"/>
              <line x1="16" y1="5" x2="16" y2="27" stroke="#38BDF8" stroke-width="1.2" opacity="0.8"/>
              <line x1="5" y1="16" x2="27" y2="16" stroke="#38BDF8" stroke-width="1.2" opacity="0.8"/>
              <circle cx="16" cy="16" r="2.7" fill="#F59E0B"/>
            </svg>
          </div>
          <span class="gf-brand-name" data-role="brand-name">GridForge</span>
          <span class="gf-brand-sub" data-role="brand-sub">Studio</span>
        </div>
      </div>

      <div class="gf-telemetry-strip" role="region" aria-label="Sheet telemetry">
        <button
          type="button"
          class="gf-telemetry-item gf-telemetry-btn gf-tel-mgrs"
          data-role="mgrs-box"
          title="Click to copy 10m MGRS coordinate"
          aria-label="Copy 10m MGRS coordinate"
        >
          <span class="gf-telemetry-label">MGRS</span>
          <span class="gf-telemetry-value cyan" data-role="mgrs">—</span>
          ${ICON_COPY}
        </button>
        <div class="gf-telemetry-item gf-tel-utm">
          <span class="gf-telemetry-label">UTM</span>
          <span class="gf-telemetry-value" data-role="utm">—</span>
        </div>
        <div class="gf-telemetry-item gf-tel-sheet">
          <span class="gf-telemetry-label">Sheet</span>
          <span class="gf-telemetry-value amber" data-role="sheet">—</span>
        </div>
        <div class="gf-telemetry-item gf-tel-gm">
          <span class="gf-telemetry-label">G-M</span>
          <span class="gf-telemetry-value" data-role="gm">—</span>
        </div>
        <div class="gf-telemetry-item gf-tel-decl">
          <span class="gf-telemetry-label">Decl</span>
          <span class="gf-telemetry-value" data-role="decl">—</span>
        </div>
      </div>

      <div class="gf-topbar-right">
        <div class="gf-topbar-group" role="group" aria-label="Workspace panels">
          <div class="gf-share-wrap" data-role="share-wrap">
            <button
              type="button"
              class="gf-topbar-btn gf-share-toggle"
              data-role="share-toggle"
              aria-haspopup="dialog"
              aria-expanded="false"
              aria-controls="gf-share-popover"
              aria-label="Share view or copy deep link"
              title="Share view or copy deep link"
            >
              ${ICON_SHARE}
              <span class="gf-btn-text">Share</span>
            </button>
            <div
              id="gf-share-popover"
              class="gf-share-popover"
              role="dialog"
              aria-label="Share map view"
              data-role="share-popover"
              hidden
            >
              <div class="gf-share-header">
                <div class="gf-share-title">Share Map View</div>
                <div class="gf-share-headline mono" data-role="share-headline">—</div>
                <div class="gf-share-detail" data-role="share-detail"></div>
              </div>
              <div class="gf-share-body">
                <div class="gf-share-note" data-role="share-note" hidden></div>
                <div class="gf-share-actions">
                  <button
                    type="button"
                    class="gf-share-action-btn"
                    data-role="share-copy-direct"
                  >
                    <span class="gf-share-action-title">Copy Direct Link</span>
                    <span class="gf-share-action-sub">Encodes settings in URL · works offline</span>
                  </button>
                  <button
                    type="button"
                    class="gf-share-action-btn gf-share-action-primary"
                    data-role="share-create-short"
                    ${callbacks.onCreateShortLink ? "" : "hidden"}
                  >
                    <span class="gf-share-action-title" data-role="share-create-short-label">Create Short Share Link</span>
                    <span class="gf-share-action-sub">Saved read-only snapshot · preserves all markups</span>
                  </button>
                </div>
                <div class="gf-share-short-box" data-role="share-short-result" hidden>
                  <div class="gf-share-short-head">
                    <span>Share Link Ready</span>
                    <span class="gf-share-short-expiry" data-role="share-short-expiry"></span>
                  </div>
                  <div class="gf-share-short-row">
                    <input
                      type="text"
                      class="gf-input mono"
                      readonly
                      data-role="share-short-input"
                      aria-label="Generated share link"
                    />
                    <button
                      type="button"
                      class="gf-btn-secondary gf-share-copy-btn"
                      data-role="share-short-copy"
                    >Copy</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <button
            type="button"
            class="gf-topbar-btn gf-projects-toggle"
            data-role="projects-toggle"
            aria-expanded="false"
            aria-label="Saved Trip Projects"
            title="Saved Trip Projects"
          >
            ${ICON_PROJECTS}
            <span class="gf-btn-text">Projects</span>
          </button>
          <button
            type="button"
            class="gf-topbar-btn gf-queue-toggle"
            data-role="queue-toggle"
            aria-expanded="false"
            aria-label="Render queue: 0, Connecting"
            title="Render queue &amp; worker status"
          >
            <span class="gf-status-dot" data-role="status-dot" aria-hidden="true"></span>
            <span class="gf-btn-text" data-role="queue-label">Queue</span>
            <span class="gf-queue-badge" data-role="queue-badge">0</span>
          </button>
        </div>
      </div>
    `;

    this.sidebarToggleBtn = this.q<HTMLButtonElement>(
      "[data-role='sidebar-toggle']"
    );
    this.brandEl = this.q("[data-role='brand']");
    this.brandNameEl = this.q("[data-role='brand-name']");
    this.brandSubEl = this.q("[data-role='brand-sub']");
    this.topbarRightEl = this.q(".gf-topbar-right");
    this.mgrsBoxBtn = this.q<HTMLButtonElement>("[data-role='mgrs-box']");
    this.mgrsEl = this.q("[data-role='mgrs']");
    this.utmEl = this.q("[data-role='utm']");
    this.sheetEl = this.q("[data-role='sheet']");
    this.gmEl = this.q("[data-role='gm']");
    this.declEl = this.q("[data-role='decl']");
    this.statusDotEl = this.q("[data-role='status-dot']");
    this.queueLabelEl = this.q("[data-role='queue-label']");
    this.shareWrapEl = this.q("[data-role='share-wrap']");
    this.shareToggleBtn = this.q<HTMLButtonElement>("[data-role='share-toggle']");
    this.sharePopoverEl = this.q("[data-role='share-popover']");
    this.shareHeadlineEl = this.q("[data-role='share-headline']");
    this.shareDetailEl = this.q("[data-role='share-detail']");
    this.shareNoteEl = this.q("[data-role='share-note']");
    this.shareCopyDirectBtn = this.q<HTMLButtonElement>(
      "[data-role='share-copy-direct']"
    );
    this.shareCreateShortBtn = this.q<HTMLButtonElement>(
      "[data-role='share-create-short']"
    );
    this.shareShortResultEl = this.q("[data-role='share-short-result']");
    this.shareShortExpiryEl = this.q("[data-role='share-short-expiry']");
    this.shareShortInput = this.q<HTMLInputElement>(
      "[data-role='share-short-input']"
    );
    this.shareShortCopyBtn = this.q<HTMLButtonElement>(
      "[data-role='share-short-copy']"
    );
    this.projectsToggleBtn = this.q<HTMLButtonElement>(
      "[data-role='projects-toggle']"
    );
    this.queueToggleBtn = this.q<HTMLButtonElement>(
      "[data-role='queue-toggle']"
    );
    this.queueBadgeEl = this.q("[data-role='queue-badge']");

    this.sidebarToggleBtn.addEventListener("click", () => {
      callbacks.onToggleSidebar?.();
    });

    this.mgrsBoxBtn.addEventListener("click", () => {
      if (this.latestMgrs) {
        callbacks.onCopyMgrs(this.latestMgrs);
      }
    });

    this.shareToggleBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const nextOpen = !this.shareMenuOpen;
      if (nextOpen) {
        this.setAccountMenuOpen(false, { restoreFocus: false });
        this.callbacks.onShareOpen?.();
      }
      this.setShareMenuOpen(nextOpen);
    });

    this.shareCopyDirectBtn.addEventListener("click", () => {
      this.callbacks.onCopyDirectLink?.();
    });

    this.shareCreateShortBtn.addEventListener("click", () => {
      if (this.shortLinkBusy) return;
      this.callbacks.onCreateShortLink?.();
    });

    this.shareShortCopyBtn.addEventListener("click", () => {
      const val = this.shareShortInput.value.trim();
      if (val) {
        this.callbacks.onCopyShareUrl?.(val);
      }
    });

    this.shareShortInput.addEventListener("focus", () => {
      this.shareShortInput.select();
    });

    this.projectsToggleBtn.addEventListener("click", () => {
      this.setShareMenuOpen(false, { restoreFocus: false });
      callbacks.onToggleProjects();
    });

    this.queueToggleBtn.addEventListener("click", () => {
      this.setShareMenuOpen(false, { restoreFocus: false });
      callbacks.onToggleDrawer();
    });

    document.addEventListener("click", this.handleDocClick);
    document.addEventListener("keydown", this.handleDocKeyDown, true);

    let injectedBrand =
      typeof window !== "undefined" ? window.__GF_BRAND__ : undefined;
    if (
      !injectedBrand &&
      typeof document !== "undefined" &&
      typeof document.getElementById === "function"
    ) {
      const brandEl = document.getElementById("gf-brand");
      if (brandEl?.textContent) {
        try {
          injectedBrand = JSON.parse(brandEl.textContent) as GridForgeBrandConfig;
        } catch {
          injectedBrand = undefined;
        }
      }
    }
    if (injectedBrand?.name || injectedBrand?.subtitle || injectedBrand?.tagline) {
      this.updateBrand(
        injectedBrand.name,
        injectedBrand.subtitle,
        injectedBrand.tagline
      );
    }
  }

  updateBrand(name?: string, subtitle?: string, tagline?: string): void {
    const cleanName = (name ?? "").trim() || "GridForge";
    const cleanSub = (subtitle ?? "").trim();
    const cleanTagline =
      (tagline ?? "").trim() || "Tactical Topographic Print Studio";

    this.brandNameEl.textContent = cleanName;
    if (cleanSub) {
      this.brandSubEl.textContent = cleanSub;
      this.brandSubEl.hidden = false;
    } else {
      this.brandSubEl.hidden = true;
    }

    this.brandEl.title =
      cleanName.toLowerCase() === "gridforge"
        ? `${cleanName} — ${cleanTagline}`
        : `${cleanName} — Powered by open-source GridForge`;

    if (typeof document !== "undefined") {
      document.title = `${cleanName} — ${cleanTagline}`;
    }
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Telemetry element not found: ${selector}`);
    return el;
  }

  private syncQueueAriaLabel(): void {
    const offlinePrefix = this.workerOffline ? "Worker offline, " : "";
    this.queueToggleBtn.setAttribute(
      "aria-label",
      `${offlinePrefix}Render queue: ${this.latestQueueCount} (${this.workerStatusLabel})`
    );
  }

  private getMenuItems(): HTMLButtonElement[] {
    if (!this.authControlsEl) return [];
    const menu = this.authControlsEl.querySelector<HTMLElement>(
      "[data-role='auth-account-menu']"
    );
    if (!menu) return [];
    return Array.from(
      menu.querySelectorAll<HTMLButtonElement>("[role='menuitem']:not(:disabled)")
    );
  }

  private setAccountMenuOpen(
    open: boolean,
    options: { focusFirst?: boolean; focusLast?: boolean; restoreFocus?: boolean } = {}
  ): void {
    this.accountMenuOpen = open;
    if (!this.authControlsEl) return;

    const trigger = this.authControlsEl.querySelector<HTMLButtonElement>(
      "[data-role='auth-account-btn']"
    );
    const menu = this.authControlsEl.querySelector<HTMLElement>(
      "[data-role='auth-account-menu']"
    );
    if (!trigger || !menu) return;

    trigger.setAttribute("aria-expanded", String(open));
    trigger.classList.toggle("menu-open", open);
    menu.hidden = !open;

    if (open) {
      this.setShareMenuOpen(false, { restoreFocus: false });
      const items = this.getMenuItems();
      const targetItem = options.focusLast
        ? items[items.length - 1]
        : options.focusFirst
          ? items[0]
          : undefined;
      targetItem?.focus();
    } else if (options.restoreFocus) {
      trigger.focus();
    }
  }

  private setShareMenuOpen(
    open: boolean,
    options: { restoreFocus?: boolean } = {}
  ): void {
    this.shareMenuOpen = open;
    this.shareToggleBtn.classList.toggle("active", open);
    this.shareToggleBtn.setAttribute("aria-expanded", String(open));
    this.sharePopoverEl.hidden = !open;
    if (!open && options.restoreFocus) {
      this.shareToggleBtn.focus();
    }
  }

  isShareMenuOpen(): boolean {
    return this.shareMenuOpen;
  }

  closeShareMenu(): void {
    if (this.shareMenuOpen) {
      this.setShareMenuOpen(false, { restoreFocus: false });
    }
  }

  setShareSummary(summary: SharePopoverSummary): void {
    this.shareHeadlineEl.textContent = summary.headline;
    this.shareDetailEl.textContent = summary.detail;
    const notes: string[] = [];
    if (summary.directTruncated) {
      notes.push(
        "Direct URL is compact and omits some markups — use Short Share Link for 100% fidelity."
      );
    }
    if (summary.hasLocalOverlayFile) {
      notes.push("Local uploaded GPX/KML files are not embedded in links.");
    }
    if (notes.length > 0) {
      this.shareNoteEl.textContent = notes.join(" ");
      this.shareNoteEl.hidden = false;
    } else {
      this.shareNoteEl.textContent = "";
      this.shareNoteEl.hidden = true;
    }
    if (summary.activeShareUrl) {
      this.shareShortInput.value = summary.activeShareUrl;
      this.shareShortExpiryEl.textContent = "Active snapshot";
      this.shareShortResultEl.hidden = false;
    }
  }

  setShortLinkBusy(busy: boolean): void {
    this.shortLinkBusy = busy;
    this.shareCreateShortBtn.disabled = busy;
    const labelEl = this.shareCreateShortBtn.querySelector(
      "[data-role='share-create-short-label']"
    );
    if (labelEl) {
      labelEl.textContent = busy
        ? "Creating Short Link…"
        : "Create Short Share Link";
    }
  }

  setShortLinkResult(result: ShortLinkDisplay | null): void {
    if (!result) {
      this.shareShortInput.value = "";
      this.shareShortExpiryEl.textContent = "";
      this.shareShortResultEl.hidden = true;
      return;
    }
    this.shareShortInput.value = result.url;
    this.shareShortExpiryEl.textContent = result.expiresLabel;
    this.shareShortResultEl.hidden = false;
  }

  resetWorkspace(): void {
    this.setAccountMenuOpen(false, { restoreFocus: false });
    this.closeShareMenu();
    this.setShareSummary({
      headline: "",
      detail: "",
      directTruncated: false,
      hasLocalOverlayFile: false,
    });
    this.setShortLinkResult(null);
    this.setShortLinkBusy(false);
    this.updateFootprint(null);
  }

  updateSidebarCollapsed(collapsed: boolean): void {
    const expanded = !collapsed;
    this.sidebarToggleBtn.classList.toggle("active", expanded);
    this.sidebarToggleBtn.setAttribute("aria-expanded", String(expanded));
    const label = collapsed ? "Show studio sidebar" : "Hide studio sidebar";
    this.sidebarToggleBtn.setAttribute("aria-label", label);
    this.sidebarToggleBtn.title = `${label} ([)`;
  }

  updateFootprint(fp: FootprintResponse | null): void {
    if (!fp) {
      this.latestMgrs = "";
      this.mgrsEl.textContent = "—";
      this.mgrsBoxBtn.setAttribute("aria-label", "Copy 10m MGRS coordinate");
      this.utmEl.textContent = "—";
      this.sheetEl.textContent = "—";
      this.gmEl.textContent = "—";
      this.declEl.textContent = "—";
      this.utmEl.removeAttribute("title");
      this.sheetEl.removeAttribute("title");
      this.gmEl.removeAttribute("title");
      this.declEl.removeAttribute("title");
      return;
    }

    this.latestMgrs = fp.center_mgrs_10m;
    this.mgrsEl.textContent = fp.center_mgrs_10m;
    this.mgrsBoxBtn.setAttribute(
      "aria-label",
      `Copy 10m MGRS coordinate ${fp.center_mgrs_10m}`
    );

    const zoneStr = escapeHtml(`${fp.utm_zone}${fp.utm_hemisphere}`);
    const epsgNum = escapeHtml(String(fp.utm_epsg));
    this.utmEl.innerHTML = `${zoneStr}<span class="gf-utm-full"> (EPSG:${epsgNum})</span><span class="gf-utm-short"> · ${epsgNum}</span>`;
    this.utmEl.title = `UTM Zone ${fp.utm_zone}${fp.utm_hemisphere} (EPSG:${fp.utm_epsg})`;

    const cols = fp.grid_cols ?? 1;
    const rows = fp.grid_rows ?? 1;
    const sheetCount = fp.sheet_count ?? cols * rows;
    if (sheetCount > 1) {
      const totalW = fp.total_ground_width_km ?? fp.ground_width_km * cols;
      const totalH = fp.total_ground_height_km ?? fp.ground_height_km * rows;
      this.sheetEl.textContent = `${cols}×${rows} · ${totalW.toFixed(1)} × ${totalH.toFixed(1)} km`;
      this.sheetEl.title = `${sheetCount} edge-matched UTM sheets (${fp.ground_width_km.toFixed(2)} × ${fp.ground_height_km.toFixed(2)} km each)`;
    } else {
      this.sheetEl.textContent = `${fp.ground_width_km.toFixed(2)} × ${fp.ground_height_km.toFixed(2)} km`;
      this.sheetEl.title = `Frame ${fp.frame_width_mm.toFixed(1)} × ${fp.frame_height_mm.toFixed(1)} mm on ${fp.page_width_mm.toFixed(0)} × ${fp.page_height_mm.toFixed(0)} mm page`;
    }

    const d = fp.declination;
    const degStr = escapeHtml(formatSignedDeg(d.gm_angle_deg));
    const milsStr = escapeHtml(formatSignedMilsNumber(d.gm_angle_mils));
    this.gmEl.innerHTML = `${degStr} (${milsStr}<span class="gf-mils-full"> mils</span><span class="gf-mils-short">m</span>)`;
    this.gmEl.title = `Grid-Magnetic angle: ${formatSignedDeg(d.gm_angle_deg)} (${formatSignedMilsNumber(d.gm_angle_mils)} NATO mils) · Grid convergence: ${formatSignedDeg(d.grid_convergence_deg)}`;

    this.declEl.textContent = formatDeclinationCompass(
      d.magnetic_declination_deg
    );
    this.declEl.title = `${d.model_name} (${d.decimal_year.toFixed(2)}) · Annual change: ${formatSignedDeg(d.annual_change_deg)}/yr`;
  }

  updateStripPlan(plan: StripPlanResponse): void {
    const first = plan.sheets[0];
    if (first?.center_mgrs_10m) {
      this.latestMgrs = first.center_mgrs_10m;
      this.mgrsEl.textContent = first.center_mgrs_10m;
      this.mgrsBoxBtn.setAttribute(
        "aria-label",
        `Copy 10m MGRS coordinate ${first.center_mgrs_10m}`
      );
    }
    const zoneStr = escapeHtml(`${plan.utm_zone}${plan.utm_hemisphere}`);
    const epsgNum = escapeHtml(String(plan.utm_epsg));
    this.utmEl.innerHTML = `${zoneStr}<span class="gf-utm-full"> (EPSG:${epsgNum})</span><span class="gf-utm-short"> · ${epsgNum}</span>`;
    this.utmEl.title = `UTM Zone ${plan.utm_zone}${plan.utm_hemisphere} (EPSG:${plan.utm_epsg})`;
    const count = plan.sheet_count ?? plan.sheets.length;
    this.sheetEl.textContent = `Strip · ${count}sh (${plan.route_distance_km.toFixed(1)} km)`;
    this.sheetEl.title = `Auto-Fit Strip Atlas: ${count} true-scale UTM sheets covering ${plan.route_distance_km.toFixed(2)} km`;
  }

  updateHealth(health: HealthResponse | null, errorMsg?: string): void {
    this.statusDotEl.className = "gf-status-dot";
    if (!health) {
      this.workerOffline = true;
      this.workerStatusLabel = errorMsg ? "API offline" : "Offline";
      this.statusDotEl.classList.add("offline");
      this.queueToggleBtn.classList.add("is-offline");
      this.queueLabelEl.textContent = "Offline";
      this.queueToggleBtn.title = errorMsg
        ? `API offline (${errorMsg}) — click to open Queue`
        : "API offline — click to open Queue";
      this.syncQueueAriaLabel();
      return;
    }

    if (health.worker_alive) {
      this.workerOffline = false;
      this.workerStatusLabel =
        health.queue_depth > 0 ? "Worker busy" : "Worker online";
      this.statusDotEl.classList.add(
        health.queue_depth > 0 ? "busy" : "online"
      );
      this.queueToggleBtn.classList.remove("is-offline");
      this.queueLabelEl.textContent = "Queue";
      this.queueToggleBtn.title = `Worker online · Queue: ${health.queue_depth} · RAM budget: ${health.memory_budget_mb} MB · Offline packs: ${health.packs_count}`;
    } else {
      this.workerOffline = true;
      this.workerStatusLabel = "Worker offline";
      this.statusDotEl.classList.add("offline");
      this.queueToggleBtn.classList.add("is-offline");
      this.queueLabelEl.textContent = "Offline";
      this.queueToggleBtn.title = health.worker_last_seen
        ? `Worker offline (last seen ${health.worker_last_seen}) · Queue: ${health.queue_depth}`
        : `Worker has not checked in yet · Queue: ${health.queue_depth}`;
    }
    this.syncQueueAriaLabel();
  }

  updateProjectsOpen(open: boolean): void {
    this.projectsToggleBtn.classList.toggle("active", open);
    this.projectsToggleBtn.setAttribute("aria-expanded", String(open));
  }

  updateQueueCounts(
    activeCount: number,
    totalCount: number,
    drawerOpen: boolean
  ): void {
    this.latestQueueCount = activeCount > 0 ? activeCount : totalCount;
    this.queueToggleBtn.classList.toggle("active", drawerOpen);
    this.queueToggleBtn.setAttribute("aria-expanded", String(drawerOpen));
    this.queueBadgeEl.textContent = String(this.latestQueueCount);
    this.queueBadgeEl.classList.toggle("has-active", activeCount > 0);
    this.syncQueueAriaLabel();
  }

  updateSession(session: SessionResponse): void {
    if (session.brand_name || session.brand_subtitle || session.brand_tagline) {
      this.updateBrand(
        session.brand_name,
        session.brand_subtitle,
        session.brand_tagline
      );
    }
    if (session.auth_mode !== "hosted") {
      this.accountMenuOpen = false;
      if (this.authDividerEl) {
        this.authDividerEl.remove();
        this.authDividerEl = null;
      }
      if (this.authControlsEl) {
        this.authControlsEl.remove();
        this.authControlsEl = null;
      }
      return;
    }

    if (!this.authDividerEl) {
      const divider = document.createElement("span");
      divider.className = "gf-topbar-divider";
      divider.setAttribute("aria-hidden", "true");
      this.topbarRightEl.appendChild(divider);
      this.authDividerEl = divider;
    }

    if (!this.authControlsEl) {
      const wrap = document.createElement("div");
      wrap.className = "gf-auth-controls";
      wrap.dataset.role = "auth-controls";

      wrap.addEventListener("click", (e) => {
        const target = e.target as HTMLElement | null;
        const btn = target?.closest<HTMLButtonElement>("button[data-role]");
        if (!btn) return;
        const role = btn.dataset.role;
        if (role === "auth-account-btn") {
          if (btn.getAttribute("aria-haspopup") === "menu") {
            e.stopPropagation();
            this.setAccountMenuOpen(!this.accountMenuOpen);
          } else {
            this.callbacks.onOpenAuth?.();
          }
        } else if (role === "auth-signin-btn") {
          this.setAccountMenuOpen(false, { restoreFocus: false });
          this.callbacks.onOpenAuth?.();
        } else if (role === "auth-open-account-btn") {
          this.setAccountMenuOpen(false, { restoreFocus: false });
          this.callbacks.onOpenAuth?.();
        } else if (role === "auth-signout-btn") {
          this.setAccountMenuOpen(false, { restoreFocus: false });
          this.callbacks.onSignOut?.();
        } else if (
          role === "admin-open-btn" ||
          role === "admin-menu-open-btn"
        ) {
          this.setAccountMenuOpen(false, { restoreFocus: false });
          this.callbacks.onOpenAdmin?.();
        }
      });

      wrap.addEventListener("keydown", (e) => {
        const target = e.target as HTMLElement | null;
        const trigger = target?.closest<HTMLButtonElement>(
          "[data-role='auth-account-btn'][aria-haspopup='menu']"
        );
        if (trigger) {
          if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
            if (!this.accountMenuOpen) {
              e.preventDefault();
              this.setAccountMenuOpen(true, { focusFirst: true });
            }
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            this.setAccountMenuOpen(true, { focusLast: true });
          }
          return;
        }

        const menuItem = target?.closest<HTMLButtonElement>("[role='menuitem']");
        if (!menuItem || !this.accountMenuOpen) return;
        const items = this.getMenuItems();
        const idx = items.indexOf(menuItem);
        if (idx < 0) return;

        if (e.key === "ArrowDown") {
          e.preventDefault();
          items[(idx + 1) % items.length]?.focus();
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          items[(idx - 1 + items.length) % items.length]?.focus();
        } else if (e.key === "Home") {
          e.preventDefault();
          items[0]?.focus();
        } else if (e.key === "End") {
          e.preventDefault();
          items[items.length - 1]?.focus();
        } else if (e.key === "Tab") {
          this.setAccountMenuOpen(false, { restoreFocus: false });
        }
      });

      this.topbarRightEl.appendChild(wrap);
      this.authControlsEl = wrap;
    }

    const wasMenuOpen = this.accountMenuOpen;
    const pill = formatAccountPill(session);

    if (!session.authenticated || session.role === "guest") {
      this.accountMenuOpen = false;
      const pillClasses = [
        "gf-auth-pill",
        "role-guest",
        pill.exhausted ? "exhausted" : "",
        pill.paused ? "paused" : "",
      ]
        .filter(Boolean)
        .join(" ");

      const quotaHtml = pill.quotaShort
        ? `<span class="gf-auth-quota mono">${escapeHtml(pill.quotaShort)}</span>`
        : "";
      const pausedTagHtml = pill.paused
        ? `<span class="gf-auth-paused-tag">Paused</span>`
        : "";

      this.authControlsEl.innerHTML = `
        <button
          type="button"
          class="${pillClasses}"
          data-role="auth-account-btn"
          aria-label="${escapeHtml(pill.ariaLabel)}"
          title="${escapeHtml(pill.title)}"
        >
          <span class="gf-auth-role">${escapeHtml(pill.roleLabel)}</span>
          ${quotaHtml}
          ${pausedTagHtml}
        </button>
        <button
          type="button"
          class="gf-topbar-btn gf-signin-btn"
          data-role="auth-signin-btn"
          aria-label="Sign in"
        >Sign In</button>
      `;
      return;
    }

    const isAdmin = session.role === "admin";
    const actorEmail = session.user?.email ?? "";
    const actorDisplay =
      session.user?.display_name?.trim() || pill.displayHandle;
    const roleClass = isAdmin ? "role-admin" : "role-friend";

    const adminShortcutHtml = isAdmin
      ? `<button
          type="button"
          class="gf-topbar-btn gf-admin-btn${pill.publicPaused ? " is-paused" : ""}"
          data-role="admin-open-btn"
          aria-label="Open admin dashboard"
          title="${pill.publicPaused ? "Public renders PAUSED — open Admin dashboard" : "Admin usage, invites &amp; kill switch"}"
        >
          ${ICON_ADMIN}
          <span class="gf-btn-text">Admin${pill.publicPaused ? " · Paused" : ""}</span>
        </button>`
      : "";

    const triggerClasses = [
      "gf-account-trigger",
      roleClass,
      pill.exhausted ? "exhausted" : "",
      wasMenuOpen ? "menu-open" : "",
    ]
      .filter(Boolean)
      .join(" ");

    const triggerQuotaHtml =
      !isAdmin && pill.quotaShort
        ? `<span class="gf-auth-quota mono">${escapeHtml(pill.quotaShort)}</span>`
        : "";

    const menuQuotaText = isAdmin
      ? pill.publicPaused
        ? "Unlimited renders · Public renders PAUSED"
        : "Unlimited renders &amp; offline packs"
      : pill.quotaFull
        ? `${escapeHtml(pill.quotaFull)} today · 14d retention`
        : "Friend tier · 14d retention";

    const adminMenuItemHtml = isAdmin
      ? `<button type="button" class="gf-account-menu-item gf-account-menu-admin" role="menuitem" data-role="admin-menu-open-btn">
          ${ICON_ADMIN}
          <span>Admin Dashboard</span>
        </button>`
      : "";

    this.authControlsEl.innerHTML = `
      ${adminShortcutHtml}
      <div class="gf-account-menu-wrap">
        <button
          type="button"
          class="${triggerClasses}"
          data-role="auth-account-btn"
          aria-haspopup="menu"
          aria-expanded="${wasMenuOpen ? "true" : "false"}"
          aria-controls="gf-account-menu"
          aria-label="${escapeHtml(pill.ariaLabel)}"
          title="${escapeHtml(pill.title)}"
        >
          <span class="gf-avatar-badge ${roleClass}" aria-hidden="true">${escapeHtml(pill.initials)}</span>
          ${triggerQuotaHtml}
          ${ICON_CHEVRON}
        </button>

        <div
          id="gf-account-menu"
          class="gf-account-menu"
          role="menu"
          aria-label="Account menu"
          data-role="auth-account-menu"
          ${wasMenuOpen ? "" : "hidden"}
        >
          <div class="gf-account-menu-header">
            <div class="gf-account-menu-title-row">
              <span class="gf-account-menu-name">${escapeHtml(actorDisplay)}</span>
              <span class="gf-account-role-tag ${roleClass}">${escapeHtml(pill.roleLabel)}</span>
            </div>
            ${actorEmail ? `<div class="gf-account-menu-email mono">${escapeHtml(actorEmail)}</div>` : ""}
            <div class="gf-account-menu-meta">${menuQuotaText}</div>
          </div>

          <div class="gf-account-menu-list">
            <button type="button" class="gf-account-menu-item" role="menuitem" data-role="auth-open-account-btn">
              <span>Account &amp; Quotas</span>
            </button>
            ${adminMenuItemHtml}
            <div class="gf-account-menu-sep" role="separator"></div>
            <button type="button" class="gf-account-menu-item gf-account-menu-danger" role="menuitem" data-role="auth-signout-btn">
              <span>Sign Out</span>
            </button>
          </div>
        </div>
      </div>
    `;
  }
}
