import "./styles.css";
import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";
import type { FeatureCollection } from "geojson";
import { renderDeclinationCompassSvg } from "./compass.ts";
import { mountMapCanvas, type MountedMapCanvas } from "./mapCanvas.ts";
import { mountRFChart } from "./rfChart.ts";
import {
  ATLAS_PRESETS,
  buildModelContextSummary,
  DEFAULT_STUDIO_PRESETS,
  escapeHtml,
  extractStructuredPayload,
  formatArtifactLabel,
  formatBytes,
  formatScale,
  formatSignedDeg,
  formatSignedMils,
  isTerminalJobStatus,
  mapPackGroupToFilter,
  RF_BAND_PRESETS,
  VALID_SCALES,
  type BriefingViewPayload,
  type ColorMode,
  type GeocodeViewPayload,
  type GridForgeViewPayload,
  type HostCapabilityFlags,
  type JobPagePayload,
  type KnownViewName,
  type Orientation,
  type PacksViewPayload,
  type PaperSize,
  type RenderViewPayload,
  type RFLinkViewPayload,
  type RouteViewPayload,
  type StudioViewPayload,
  type ViewshedViewPayload,
} from "./types.ts";

// Keep internal state strictly in memory (iframe has an opaque origin — never use localStorage/IndexedDB)
interface AppRuntimeState {
  connected: boolean;
  demoMode: boolean;
  busy: boolean;
  busyLabel: string;
  toast: { text: string; isError: boolean } | null;
  caps: HostCapabilityFlags;
  payload: GridForgeViewPayload | null;
  // Render sheet viewer state
  activePage: number;
  pageZoom: "fit" | "100" | "150" | "200";
  pageLoading: boolean;
  pageError: string | null;
  pageCache: Map<string, JobPagePayload>;
  // Packs filter state
  packsRegionFilter: "ALL" | "AU" | "NZ" | "NA";
  packsSearchQuery: string;
  // Known public base URL for resolving relative download links
  publicBaseUrl: string | null;
}

const state: AppRuntimeState = {
  connected: false,
  demoMode: false,
  busy: false,
  busyLabel: "",
  toast: null,
  caps: {
    serverTools: false,
    openLinks: false,
    message: false,
    updateModelContext: false,
  },
  payload: null,
  activePage: 1,
  pageZoom: "fit",
  pageLoading: false,
  pageError: null,
  pageCache: new Map(),
  packsRegionFilter: "ALL",
  packsSearchQuery: "",
  publicBaseUrl: null,
};

let activeMap: MountedMapCanvas | null = null;
let activeChartCleanup: (() => void) | null = null;
let pollTimer: number | null = null;
let pollInFlight = false;
let toastTimer: number | null = null;

const app = new App(
  { name: "GridForge", version: "1.7.1" },
  {},
  { autoResize: true }
);

const LOGO_SVG = `<svg width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden="true">
  <rect x="2.5" y="2.5" width="15" height="15" rx="3" stroke="currentColor" stroke-width="1.6"/>
  <path d="M10 2.5V17.5M2.5 10H17.5" stroke="currentColor" stroke-width="1.2" opacity="0.55"/>
  <circle cx="10" cy="10" r="2.7" stroke="currentColor" stroke-width="1.6"/>
</svg>`;

function getRootEl(): HTMLElement {
  return document.getElementById("mcp-app") as HTMLElement;
}

function cleanupInteractiveMounts(): void {
  if (activeMap) {
    activeMap.destroy();
    activeMap = null;
  }
  if (activeChartCleanup) {
    activeChartCleanup();
    activeChartCleanup = null;
  }
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
  pollInFlight = false;
}

function showToast(text: string, isError = false): void {
  state.toast = { text, isError };
  if (toastTimer !== null) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    state.toast = null;
    const toastEl = document.querySelector<HTMLElement>("[data-gf-toast]");
    if (toastEl) toastEl.remove();
  }, 4500);
  renderCurrentView();
}

function isSafeHttpUrl(url: string | null | undefined): boolean {
  if (!url || typeof url !== "string") return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function resolveExternalUrl(rawUrl: string | null | undefined): string | null {
  if (!rawUrl || typeof rawUrl !== "string") return null;
  if (isSafeHttpUrl(rawUrl)) return rawUrl;
  if (rawUrl.startsWith("/") && state.publicBaseUrl) {
    try {
      const resolved = new URL(rawUrl, state.publicBaseUrl).toString();
      return isSafeHttpUrl(resolved) ? resolved : null;
    } catch {
      return null;
    }
  }
  return null;
}

function rememberBaseUrlFromPayload(payload: GridForgeViewPayload): void {
  const candidates: Array<string | null | undefined> = [];
  if ("public_url" in payload) candidates.push(payload.public_url);
  if ("studio_url" in payload) candidates.push(payload.studio_url);
  if ("share_url" in payload) candidates.push(payload.share_url);
  for (const c of candidates) {
    if (c && isSafeHttpUrl(c)) {
      try {
        state.publicBaseUrl = new URL(c).origin;
        return;
      } catch (err) {
        console.debug("Ignoring malformed base URL candidate:", c, err);
      }
    }
  }
}

function applyHostContext(ctx: McpUiHostContext | undefined): void {
  if (!ctx) return;
  if (ctx.theme) {
    applyDocumentTheme(ctx.theme);
  }
  if (ctx.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }
  if (ctx.styles?.css?.fonts) {
    applyHostFonts(ctx.styles.css.fonts);
  }
  activeMap?.redraw();
}

function syncCapabilities(): void {
  if (state.demoMode) {
    state.caps = {
      serverTools: true,
      openLinks: true,
      message: true,
      updateModelContext: true,
    };
    return;
  }
  const hostCaps = app.getHostCapabilities();
  state.caps = {
    serverTools: Boolean(hostCaps?.serverTools),
    openLinks: Boolean(hostCaps?.openLinks),
    message: Boolean(hostCaps?.message),
    updateModelContext: Boolean(hostCaps?.updateModelContext),
  };
}

async function openExternalUrl(url: string | null | undefined, label = "link"): Promise<void> {
  const resolved = resolveExternalUrl(url);
  if (!resolved) {
    showToast(`No valid public URL available for ${label}.`, true);
    return;
  }
  if (state.demoMode) {
    showToast(`Demo preview would open: ${resolved}`);
    return;
  }
  if (!state.caps.openLinks) {
    showToast(`Host cannot open links directly: ${resolved}`);
    return;
  }
  try {
    const res = await app.openLink({ url: resolved });
    if (res?.isError) {
      showToast(`Host declined opening ${label}: ${resolved}`, true);
    }
  } catch (err) {
    showToast(`Could not open ${label}: ${err instanceof Error ? err.message : String(err)}`, true);
  }
}

async function attachPayloadToChat(): Promise<void> {
  if (!state.payload) return;
  const summary = buildModelContextSummary(state.payload);
  if (state.demoMode) {
    showToast("Demo: attached tactical telemetry context for your next chat message.");
    return;
  }
  if (!state.caps.updateModelContext) return;
  try {
    await app.updateModelContext({
      content: [{ type: "text", text: summary }],
    });
    showToast("Attached current GridForge state to your next message.");
  } catch (err) {
    showToast(`Could not attach context: ${err instanceof Error ? err.message : String(err)}`, true);
  }
}

async function askFollowUpInChat(promptText: string): Promise<void> {
  if (state.demoMode) {
    showToast(`Demo follow-up prompt: "${promptText}"`);
    return;
  }
  if (!state.caps.message) return;
  try {
    if (state.caps.updateModelContext && state.payload) {
      await app.updateModelContext({
        content: [{ type: "text", text: buildModelContextSummary(state.payload) }],
      });
    }
    const res = await app.sendMessage({
      role: "user",
      content: [{ type: "text", text: promptText }],
    });
    if (res?.isError) {
      showToast("Host declined sending follow-up message.", true);
    }
  } catch (err) {
    showToast(`Follow-up failed: ${err instanceof Error ? err.message : String(err)}`, true);
  }
}

async function openInStudioAction(
  studioUrl: string | null | undefined,
  shareUrl: string | null | undefined,
  snapshotPayload?: Record<string, unknown>
): Promise<void> {
  const direct = resolveExternalUrl(shareUrl) || resolveExternalUrl(studioUrl);
  if (direct) {
    await openExternalUrl(direct, "GridForge Studio");
    return;
  }
  if (!state.caps.serverTools || !snapshotPayload) {
    showToast("Configure GRIDFORGE_PUBLIC_URL to enable Studio deep links.", true);
    return;
  }
  try {
    state.busy = true;
    state.busyLabel = "Creating Studio share link…";
    renderCurrentView();
    const res = await callTool("create_share_link", snapshotPayload);
    const sc = (res as { structuredContent?: Record<string, unknown> })?.structuredContent;
    const nextUrl =
      (sc?.share_url as string | undefined) ||
      (sc?.studio_url as string | undefined) ||
      (sc?.share_path as string | undefined);
    state.busy = false;
    renderCurrentView();
    await openExternalUrl(nextUrl, "GridForge Studio");
  } catch (err) {
    state.busy = false;
    showToast(`Share link failed: ${err instanceof Error ? err.message : String(err)}`, true);
  }
}

async function callTool(
  name: string,
  args: Record<string, unknown>
): Promise<unknown> {
  if (state.demoMode) {
    return runDemoTool(name, args);
  }
  if (!state.caps.serverTools) {
    throw new Error("This host surface does not allow server tool calls.");
  }
  return app.callServerTool({ name, arguments: args });
}

async function invokeViewTool(
  toolName: string,
  args: Record<string, unknown>,
  busyLabel: string
): Promise<void> {
  if (state.busy) return;
  state.busy = true;
  state.busyLabel = busyLabel;
  state.toast = null;
  renderCurrentView();
  try {
    const res = await callTool(toolName, args);
    state.busy = false;
    handleToolResultPayload(res);
  } catch (err) {
    state.busy = false;
    showToast(err instanceof Error ? err.message : String(err), true);
  }
}

function handleToolResultPayload(result: unknown): void {
  const r = result as { isError?: boolean; content?: Array<{ type?: string; text?: string }> };
  if (r?.isError) {
    const errText =
      r.content?.find((c) => c.type === "text" && c.text)?.text ||
      "Tool execution failed.";
    showToast(errText, true);
    return;
  }
  const parsed = extractStructuredPayload(result);
  if (!parsed) {
    const fallbackText = r?.content?.find((c) => c.type === "text" && c.text)?.text;
    if (fallbackText) {
      showToast(fallbackText.slice(0, 180));
    }
    return;
  }
  setPayload(parsed);
}

function setPayload(payload: GridForgeViewPayload): void {
  const prevJobId = state.payload?.view === "render" ? state.payload.job.job_id : null;
  stopPolling();
  rememberBaseUrlFromPayload(payload);
  state.payload = payload;

  if (payload.view === "render") {
    if (payload.job.job_id !== prevJobId) {
      state.activePage = 1;
      state.pageZoom = "fit";
      state.pageError = null;
    }
    if (state.activePage > Math.max(1, payload.page_count || 1)) {
      state.activePage = 1;
    }
  }

  renderCurrentView();

  if (payload.view === "render") {
    if (!isTerminalJobStatus(payload.job.status)) {
      startRenderPolling(payload.job.job_id);
    } else if (payload.job.status === "succeeded") {
      void ensureRenderPageLoaded(payload.job.job_id, state.activePage);
    }
  }
}

function startRenderPolling(jobId: string): void {
  stopPolling();
  if (!state.caps.serverTools) return;

  const pollOnce = async () => {
    if (pollInFlight) return;
    if (state.payload?.view !== "render" || state.payload.job.job_id !== jobId) {
      stopPolling();
      return;
    }
    pollInFlight = true;
    try {
      const res = await callTool("app_get_job_status", { job_id: jobId });
      const sc = (res as { structuredContent?: Record<string, unknown> })?.structuredContent;
      if (sc && sc.job && state.payload?.view === "render" && state.payload.job.job_id === jobId) {
        const nextJob = sc.job as RenderViewPayload["job"];
        const nextCount = typeof sc.page_count === "number" ? sc.page_count : state.payload.page_count;
        const nextPages = Array.isArray(sc.pages)
          ? (sc.pages as RenderViewPayload["pages"])
          : state.payload.pages;
        state.payload = {
          ...state.payload,
          job: nextJob,
          page_count: nextCount,
          pages: nextPages,
        };
        if (isTerminalJobStatus(nextJob.status)) {
          stopPolling();
          renderCurrentView();
          if (nextJob.status === "succeeded") {
            void ensureRenderPageLoaded(jobId, state.activePage);
          }
        } else {
          renderCurrentView();
        }
      }
    } catch (err) {
      // Transient polling error — keep polling next interval
      console.warn("Transient GridForge job poll error; retrying:", err);
    } finally {
      pollInFlight = false;
    }
  };

  pollTimer = window.setInterval(() => {
    void pollOnce();
  }, 2000);
}

async function ensureRenderPageLoaded(jobId: string, page: number): Promise<void> {
  const key = `${jobId}:${page}`;
  if (state.pageCache.has(key)) {
    state.pageLoading = false;
    state.pageError = null;
    renderCurrentView();
    return;
  }
  if (!state.caps.serverTools) return;
  state.pageLoading = true;
  state.pageError = null;
  renderCurrentView();

  try {
    const res = await callTool("app_get_job_page", {
      job_id: jobId,
      page,
      width: 1100,
    });
    const sc = (res as { structuredContent?: JobPagePayload })?.structuredContent;
    if (sc && typeof sc.data_base64 === "string" && sc.data_base64.length > 0) {
      state.pageCache.set(key, sc);
      if (
        state.payload?.view === "render" &&
        state.payload.job.job_id === jobId &&
        typeof sc.page_count === "number" &&
        sc.page_count > 0
      ) {
        state.payload = { ...state.payload, page_count: sc.page_count };
      }
      state.pageLoading = false;
      state.pageError = null;
    } else {
      state.pageLoading = false;
      state.pageError = "No PNG preview available for this page.";
    }
  } catch (err) {
    state.pageLoading = false;
    state.pageError = err instanceof Error ? err.message : "Could not load page preview.";
  }
  renderCurrentView();
}

/* ============================================================================
 * Shared UI Helpers
 * ============================================================================ */

function renderHeader(
  title: string,
  subtitle: string,
  rightHtml = ""
): string {
  return `
    <header class="gf-header">
      <div class="gf-brand-row">
        <div class="gf-logo">${LOGO_SVG}</div>
        <div class="gf-title-wrap">
          <h1 class="gf-title">${escapeHtml(title)}</h1>
          <p class="gf-subtitle">${escapeHtml(subtitle)}</p>
        </div>
      </div>
      <div class="gf-header-right">
        ${state.busy ? `<span class="gf-badge gf-badge-info">${escapeHtml(state.busyLabel || "Working…")}</span>` : ""}
        ${rightHtml}
      </div>
    </header>
  `;
}

function renderToastBanner(): string {
  if (!state.toast) return "";
  return `
    <div class="gf-toast ${state.toast.isError ? "gf-toast-error" : ""}" data-gf-toast role="status">
      <span>${escapeHtml(state.toast.text)}</span>
      <button type="button" class="gf-btn gf-btn-sm" data-dismiss-toast aria-label="Dismiss">✕</button>
    </div>
  `;
}

function renderHostConversationButtons(followUpLabel: string): string {
  const parts: string[] = [];
  if (state.caps.updateModelContext) {
    parts.push(
      `<button type="button" class="gf-btn" data-action="attach-context" ${state.busy ? "disabled" : ""}>Attach to Chat</button>`
    );
  }
  if (state.caps.message) {
    parts.push(
      `<button type="button" class="gf-btn" data-action="ask-followup" ${state.busy ? "disabled" : ""}>${escapeHtml(followUpLabel)}</button>`
    );
  }
  return parts.join("");
}

function selectOptions(
  items: ReadonlyArray<{ value: string; label: string }>,
  current: string
): string {
  return items
    .map(
      (it) =>
        `<option value="${escapeHtml(it.value)}" ${it.value === current ? "selected" : ""}>${escapeHtml(it.label)}</option>`
    )
    .join("");
}

/* ============================================================================
 * View 1: Studio (`open_gridforge_studio`)
 * ============================================================================ */

function renderStudioView(p: StudioViewPayload): string {
  const presets = p.presets && p.presets.length > 0 ? p.presets : DEFAULT_STUDIO_PRESETS;
  const packs = p.packs ?? [];
  const scaleOpts = VALID_SCALES.map((s) => ({ value: String(s), label: formatScale(s) }));
  const themeOpts = [
    { value: "color", label: "Color Topo" },
    { value: "alpine", label: "Alpine" },
    { value: "bw", label: "B&W Print" },
    { value: "night-red", label: "Night-Red" },
  ];
  const paperOpts = [
    { value: "a4", label: "A4" },
    { value: "letter", label: "Letter" },
    { value: "a3", label: "A3" },
    { value: "legal", label: "Legal" },
  ];
  const orientOpts = [
    { value: "portrait", label: "Portrait" },
    { value: "landscape", label: "Landscape" },
  ];
  const atlasOpts = ATLAS_PRESETS.map((a) => ({ value: a.id, label: a.label }));

  const openStudioBtn =
    state.caps.openLinks && (p.share_url || p.public_url)
      ? `<button type="button" class="gf-btn gf-btn-sm" data-action="open-studio">Open Full Studio ↗</button>`
      : "";

  return `
    ${renderHeader(
      p.title || "GridForge Studio",
      p.summary || "True-scale local UTM topographic maps, MGRS, WMM2025 declination & RF LOS",
      `
        <span class="gf-badge gf-badge-neutral">${packs.length} Offline Pack${packs.length === 1 ? "" : "s"}</span>
        ${openStudioBtn}
      `
    )}
    ${renderToastBanner()}

    <section class="gf-card">
      <div class="gf-section-title">
        <span>Search Location or Jump to Coordinates</span>
        <span class="mono">Place name · lat,lon · MGRS</span>
      </div>
      <form class="gf-search-row" data-studio-search-form>
        <input type="text" class="gf-input" name="query" placeholder="e.g. Mt Kosciuszko, Yosemite Valley, -33.715,150.312, or 56HLH4352169450" autocomplete="off" />
        <button type="submit" class="gf-btn gf-btn-secondary" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Search</button>
        <button type="button" class="gf-btn gf-btn-primary" data-studio-brief-direct ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Brief</button>
      </form>
    </section>

    <section class="gf-card">
      <div class="gf-section-title">
        <span>Quick-Jump Regional Presets</span>
        <span>Click for MGRS, declination & live sheet preview</span>
      </div>
      <div class="gf-preset-grid">
        ${presets
          .map(
            (pr) => `
          <button type="button" class="gf-preset-card" data-preset-id="${escapeHtml(pr.id)}" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>
            <span class="gf-preset-name">${escapeHtml(pr.name)}</span>
            <span class="gf-preset-meta">
              <span>${escapeHtml(pr.region)}</span>
              <span class="mono">${escapeHtml(formatScale(pr.scale))} · ${escapeHtml(pr.paper.toUpperCase())}</span>
            </span>
          </button>
        `
          )
          .join("")}
      </div>
    </section>

    <section class="gf-card">
      <div class="gf-section-title">
        <span>Quick-Build Printable Map</span>
        <span>True-Scale Local UTM</span>
      </div>
      <form data-studio-build-form>
        <div class="gf-form-grid">
          <div class="gf-field" style="grid-column: span 2;">
            <label for="st-center">Center (Place, lat,lon, or MGRS)</label>
            <input id="st-center" name="center" class="gf-input mono" value="${escapeHtml(presets[0]?.center || "-33.7152, 150.3119")}" />
          </div>
          <div class="gf-field">
            <label for="st-scale">Scale</label>
            <select id="st-scale" name="scale" class="gf-select">${selectOptions(scaleOpts, "25000")}</select>
          </div>
          <div class="gf-field">
            <label for="st-theme">Theme</label>
            <select id="st-theme" name="color_mode" class="gf-select">${selectOptions(themeOpts, "color")}</select>
          </div>
          <div class="gf-field">
            <label for="st-paper">Paper</label>
            <select id="st-paper" name="paper" class="gf-select">${selectOptions(paperOpts, "a4")}</select>
          </div>
          <div class="gf-field">
            <label for="st-orient">Orientation</label>
            <select id="st-orient" name="orientation" class="gf-select">${selectOptions(orientOpts, "portrait")}</select>
          </div>
          <div class="gf-field">
            <label for="st-atlas">Atlas Grid</label>
            <select id="st-atlas" name="atlas" class="gf-select">${selectOptions(atlasOpts, "1x1")}</select>
          </div>
        </div>
        <div class="gf-actions">
          <button type="submit" class="gf-btn gf-btn-secondary" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Preview Footprint</button>
          <button type="button" class="gf-btn gf-btn-primary" data-studio-render ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Render Printable PDF</button>
          ${
            state.caps.serverTools
              ? `<button type="button" class="gf-btn" data-action="browse-packs" ${state.busy ? "disabled" : ""}>Offline Packs (${packs.length})</button>`
              : ""
          }
        </div>
      </form>
    </section>

    ${
      packs.length > 0
        ? `
      <section class="gf-card">
        <div class="gf-section-title">
          <span>Installed Air-Gapped Regional Packs</span>
          <span>Zero-network rendering ready</span>
        </div>
        <div class="gf-pill-row" style="margin-top:0">
          ${packs
            .map(
              (pk) =>
                `<span class="gf-chip gf-chip-emerald">${escapeHtml(pk.name)} · ${escapeHtml(formatBytes(pk.size_bytes))}</span>`
            )
            .join("")}
        </div>
      </section>
    `
        : ""
    }
  `;
}

/* ============================================================================
 * View 2: Location Briefing & Footprint Explorer (`briefing`)
 * ============================================================================ */

function renderBriefingView(p: BriefingViewPayload): string {
  const loc = p.location;
  const sh = p.sheet;
  const dec = p.declination;
  const rec = p.recommendations;
  const atlasKey = `${sh.grid_cols || 1}x${sh.grid_rows || 1}`;
  const totalW = sh.total_ground_width_km ?? sh.ground_width_km * (sh.grid_cols || 1);
  const totalH = sh.total_ground_height_km ?? sh.ground_height_km * (sh.grid_rows || 1);

  const scaleOpts = VALID_SCALES.map((s) => ({ value: String(s), label: formatScale(s) }));
  const themeOpts = [
    { value: "color", label: "Color" },
    { value: "bw", label: "B&W" },
    { value: "night-red", label: "Night-Red" },
    { value: "alpine", label: "Alpine" },
  ];
  const paperOpts = [
    { value: "a4", label: "A4" },
    { value: "letter", label: "Letter" },
    { value: "a3", label: "A3" },
    { value: "legal", label: "Legal" },
  ];
  const orientOpts = [
    { value: "portrait", label: "Portrait" },
    { value: "landscape", label: "Landscape" },
  ];
  const atlasOpts = ATLAS_PRESETS.map((a) => ({ value: a.id, label: a.label }));
  const dpiOpts = [
    { value: "300", label: "300 DPI" },
    { value: "450", label: "450 DPI" },
    { value: "600", label: "600 DPI" },
  ];

  const hasStudioLink =
    state.caps.openLinks &&
    Boolean(resolveExternalUrl(p.share_url) || resolveExternalUrl(p.studio_url) || state.caps.serverTools);

  return `
    ${renderHeader(
      p.title || loc.place_name || `Briefing · ${loc.mgrs_10m}`,
      p.summary || `${formatScale(sh.scale)} · ${sh.paper.toUpperCase()} ${sh.orientation} · EPSG:${loc.utm_epsg}`,
      `
        <span class="gf-badge gf-badge-info mono">${escapeHtml(loc.mgrs_10m)}</span>
        <span class="gf-badge gf-badge-neutral mono">UTM ${loc.utm_zone}${escapeHtml(loc.utm_hemisphere)}</span>
      `
    )}
    ${renderToastBanner()}

    <div class="gf-briefing-top">
      <div class="gf-kpi-strip">
        <div class="gf-kpi-tile gf-kpi-tile-cyan">
          <div class="gf-kpi-label">10m MGRS Grid</div>
          <div class="gf-kpi-value mono">${escapeHtml(loc.mgrs_10m)}</div>
          <div class="gf-kpi-sub mono">${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)}</div>
        </div>
        <div class="gf-kpi-tile">
          <div class="gf-kpi-label">Local UTM CRS</div>
          <div class="gf-kpi-value mono">EPSG:${loc.utm_epsg}</div>
          <div class="gf-kpi-sub">GZD ${escapeHtml(loc.gzd)} · 100k ${escapeHtml((loc.square_ids_100km || []).join(", ") || "—")}</div>
        </div>
        <div class="gf-kpi-tile gf-kpi-tile-amber">
          <div class="gf-kpi-label">Ground Footprint</div>
          <div class="gf-kpi-value mono">${totalW.toFixed(2)} × ${totalH.toFixed(2)} km</div>
          <div class="gf-kpi-sub">${escapeHtml(formatScale(sh.scale))} · ${sh.sheet_count || 1} sheet${(sh.sheet_count || 1) === 1 ? "" : "s"} (${sh.grid_spacing_mm.toFixed(1)}mm grid)</div>
        </div>
        <div class="gf-kpi-tile gf-kpi-tile-emerald">
          <div class="gf-kpi-label">${escapeHtml(dec.model_name || "WMM2025")} (${dec.decimal_year.toFixed(1)})</div>
          <div class="gf-kpi-value mono">G-M ${escapeHtml(formatSignedDeg(dec.gm_angle_deg))}</div>
          <div class="gf-kpi-sub mono">${escapeHtml(formatSignedMils(dec.gm_angle_mils))} · Mag ${escapeHtml(formatSignedDeg(dec.magnetic_declination_deg))}</div>
        </div>
      </div>
      <div class="gf-compass-box" title="WMM2025 True / Grid / Magnetic North Diagram">
        ${renderDeclinationCompassSvg(dec, { compact: true })}
      </div>
    </div>

    ${
      p.answer || p.question || rec
        ? `
      <section class="gf-answer-card">
        ${p.question ? `<div class="gf-answer-question">${escapeHtml(p.question)}</div>` : ""}
        ${p.answer ? `<p class="gf-answer-text">${escapeHtml(p.answer)}</p>` : ""}
        ${
          rec
            ? `
          <div class="gf-pill-row">
            <span class="gf-chip gf-chip-cyan">DEM: ${escapeHtml(rec.dem_source)}</span>
            ${rec.region_label ? `<span class="gf-chip">${escapeHtml(rec.region_label)}</span>` : ""}
            ${(rec.overlays || [])
              .map((ov) => `<span class="gf-chip gf-chip-amber">+ ${escapeHtml(ov)}</span>`)
              .join("")}
            ${(rec.matching_packs || [])
              .map((pk) => `<span class="gf-chip gf-chip-emerald">Pack: ${escapeHtml(pk)}</span>`)
              .join("")}
          </div>
        `
            : ""
        }
      </section>
    `
        : ""
    }

    <div class="gf-split-layout">
      <div class="gf-stack">
        <div data-map-mount></div>
      </div>

      <section class="gf-card gf-stack">
        <div class="gf-section-title">
          <span>Live Sheet Controls</span>
          <span class="mono">${sh.page_width_mm.toFixed(0)} × ${sh.page_height_mm.toFixed(0)} mm</span>
        </div>
        <form data-briefing-form>
          <div class="gf-form-grid">
            <div class="gf-field">
              <label for="bf-scale">Scale</label>
              <select id="bf-scale" name="scale" class="gf-select">${selectOptions(scaleOpts, String(sh.scale))}</select>
            </div>
            <div class="gf-field">
              <label for="bf-theme">Theme</label>
              <select id="bf-theme" name="color_mode" class="gf-select">${selectOptions(themeOpts, sh.color_mode || "color")}</select>
            </div>
            <div class="gf-field">
              <label for="bf-paper">Paper</label>
              <select id="bf-paper" name="paper" class="gf-select">${selectOptions(paperOpts, sh.paper || "a4")}</select>
            </div>
            <div class="gf-field">
              <label for="bf-orient">Orientation</label>
              <select id="bf-orient" name="orientation" class="gf-select">${selectOptions(orientOpts, sh.orientation || "portrait")}</select>
            </div>
            <div class="gf-field">
              <label for="bf-atlas">Atlas Grid</label>
              <select id="bf-atlas" name="atlas" class="gf-select">${selectOptions(atlasOpts, atlasKey)}</select>
            </div>
            <div class="gf-field">
              <label for="bf-dpi">Print DPI</label>
              <select id="bf-dpi" name="dpi" class="gf-select">${selectOptions(dpiOpts, String(sh.dpi || 300))}</select>
            </div>
          </div>

          <div class="gf-actions">
            <button type="submit" class="gf-btn gf-btn-secondary" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Update Footprint</button>
            <button type="button" class="gf-btn gf-btn-primary" data-briefing-render ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Render Printable PDF</button>
            ${
              hasStudioLink
                ? `<button type="button" class="gf-btn" data-action="open-studio" ${state.busy ? "disabled" : ""}>Open in Studio ↗</button>`
                : ""
            }
            ${renderHostConversationButtons("Ask Follow-up")}
          </div>
        </form>
      </section>
    </div>
  `;
}

/* ============================================================================
 * View 3: RF Line-of-Sight & 1st Fresnel Link Analysis (`rf_link`)
 * ============================================================================ */

function renderRFLinkView(p: RFLinkViewPayload): string {
  const pr = p.profile;
  const req = p.request;
  const badgeClass =
    pr.status === "CLEAR"
      ? "gf-badge-clear"
      : pr.status === "MARGINAL"
        ? "gf-badge-marginal"
        : "gf-badge-obstructed";
  const clearSign = pr.min_clearance_m >= 0 ? "+" : "";
  const activeFreq = Math.round(req.freq_mhz || pr.freq_mhz || 915);

  const hasStudioLink =
    state.caps.openLinks &&
    Boolean(resolveExternalUrl(p.share_url) || resolveExternalUrl(p.studio_url));

  return `
    ${renderHeader(
      p.title || `RF Link · ${pr.from_label} → ${pr.to_label}`,
      p.summary || `${pr.from_mgrs} → ${pr.to_mgrs} · ${pr.freq_mhz} MHz Curved-Earth 1st Fresnel`,
      `<span class="gf-badge ${badgeClass}">${escapeHtml(pr.status)}</span>`
    )}
    ${renderToastBanner()}

    <div class="gf-kpi-strip">
      <div class="gf-kpi-tile gf-kpi-tile-cyan">
        <div class="gf-kpi-label">Path Distance</div>
        <div class="gf-kpi-value mono">${pr.distance_km.toFixed(2)} km</div>
        <div class="gf-kpi-sub mono">${pr.freq_mhz} MHz · k=4/3 Earth</div>
      </div>
      <div class="gf-kpi-tile">
        <div class="gf-kpi-label">Free-Space Path Loss</div>
        <div class="gf-kpi-value mono">${pr.fspl_db.toFixed(1)} dB</div>
        <div class="gf-kpi-sub">LOS ${pr.los_clear ? "Clear" : "Blocked"} · 60% F1 ${pr.fresnel_60pct_clear ? "OK" : "Margin"}</div>
      </div>
      <div class="gf-kpi-tile ${pr.status === "CLEAR" ? "gf-kpi-tile-emerald" : "gf-kpi-tile-amber"}">
        <div class="gf-kpi-label">Min Clearance</div>
        <div class="gf-kpi-value mono">${clearSign}${pr.min_clearance_m.toFixed(1)} m</div>
        <div class="gf-kpi-sub mono">${pr.min_fresnel_pct.toFixed(0)}% of 1st Fresnel Zone</div>
      </div>
      <div class="gf-kpi-tile">
        <div class="gf-kpi-label">Azimuth (TX → RX)</div>
        <div class="gf-kpi-value mono">T ${pr.bearing_true_deg.toFixed(1)}° · M ${pr.bearing_mag_deg.toFixed(1)}°</div>
        <div class="gf-kpi-sub mono">G ${pr.bearing_grid_deg.toFixed(1)}° · ${Math.round(pr.bearing_mils)} mils</div>
      </div>
    </div>

    ${
      p.answer
        ? `<section class="gf-answer-card"><p class="gf-answer-text">${escapeHtml(p.answer)}</p></section>`
        : ""
    }

    <section class="gf-card">
      <div class="gf-section-title">
        <span>Curved-Earth 1st Fresnel Elevation Profile</span>
        <span class="mono">${escapeHtml(pr.from_label)} (${pr.from_elev_m != null ? Math.round(pr.from_elev_m) + "m" : "—"}) → ${escapeHtml(pr.to_label)} (${pr.to_elev_m != null ? Math.round(pr.to_elev_m) + "m" : "—"})</span>
      </div>
      <div data-rf-mount></div>
    </section>

    <section class="gf-card">
      <div class="gf-section-title">
        <span>RF Band & Antenna Mast Parameters</span>
        <span>Recalculate live against DEM</span>
      </div>
      <form data-rf-form>
        <div class="gf-band-pills" role="group" aria-label="Radio band presets">
          ${RF_BAND_PRESETS.map(
            (b) => `
            <button type="button" class="gf-pill-btn ${Math.abs(b.freq_mhz - activeFreq) < 2 ? "active" : ""}" data-rf-band="${b.freq_mhz}">${escapeHtml(b.label)}</button>
          `
          ).join("")}
        </div>
        <div class="gf-form-grid">
          <div class="gf-field">
            <label for="rf-freq">Frequency (MHz)</label>
            <input id="rf-freq" name="freq_mhz" type="number" step="0.1" min="30" max="10000" class="gf-input mono" value="${req.freq_mhz || pr.freq_mhz}" />
          </div>
          <div class="gf-field">
            <label for="rf-tx-h">${escapeHtml(pr.from_label || "TX")} Height (m)</label>
            <input id="rf-tx-h" name="from_height_m" type="number" step="0.5" min="0.5" max="200" class="gf-input mono" value="${req.from_height_m ?? pr.from_height_m}" />
          </div>
          <div class="gf-field">
            <label for="rf-rx-h">${escapeHtml(pr.to_label || "RX")} Height (m)</label>
            <input id="rf-rx-h" name="to_height_m" type="number" step="0.5" min="0.5" max="200" class="gf-input mono" value="${req.to_height_m ?? pr.to_height_m}" />
          </div>
        </div>
        <div class="gf-actions">
          <button type="submit" class="gf-btn gf-btn-secondary" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Recalculate Link</button>
          <button type="button" class="gf-btn gf-btn-primary" data-rf-render ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Render Link Card PDF</button>
          ${
            hasStudioLink
              ? `<button type="button" class="gf-btn" data-action="open-studio">Open in Studio ↗</button>`
              : ""
          }
          ${renderHostConversationButtons("Ask Follow-up")}
        </div>
      </form>
    </section>
  `;
}

/* ============================================================================
 * View 4: Terrain-Masked Viewshed Coverage (`viewshed`)
 * ============================================================================ */

function renderViewshedView(p: ViewshedViewPayload): string {
  const obs = p.observer;
  const res = p.result;
  const radiusKm = (res.max_distance_m || obs.max_distance_m) / 1000;
  const discArea = Math.PI * radiusKm * radiusKm;
  const pctVisible = discArea > 0 ? Math.min(100, (res.visible_area_km2 / discArea) * 100) : 0;

  const hasStudioLink =
    state.caps.openLinks &&
    Boolean(resolveExternalUrl(p.share_url) || resolveExternalUrl(p.studio_url));

  return `
    ${renderHeader(
      p.title || `Viewshed · ${obs.label || "Observer"}`,
      p.summary || `Terrain-masked LOS coverage · EPSG:${res.utm_epsg}`,
      `<span class="gf-badge gf-badge-clear mono">${res.visible_area_km2.toFixed(1)} km² Visible</span>`
    )}
    ${renderToastBanner()}

    <div class="gf-kpi-strip">
      <div class="gf-kpi-tile gf-kpi-tile-cyan">
        <div class="gf-kpi-label">Observer MGRS</div>
        <div class="gf-kpi-value mono">${escapeHtml(res.observer_mgrs_10m)}</div>
        <div class="gf-kpi-sub mono">${obs.lat.toFixed(5)}, ${obs.lon.toFixed(5)}</div>
      </div>
      <div class="gf-kpi-tile">
        <div class="gf-kpi-label">Observer Elevation</div>
        <div class="gf-kpi-value mono">${res.observer_elev_m != null ? `${Math.round(res.observer_elev_m)} m` : "—"}</div>
        <div class="gf-kpi-sub">Mast +${obs.observer_height_m}m · Target +${obs.target_height_m}m</div>
      </div>
      <div class="gf-kpi-tile gf-kpi-tile-emerald">
        <div class="gf-kpi-label">Visible Area</div>
        <div class="gf-kpi-value mono">${res.visible_area_km2.toFixed(2)} km²</div>
        <div class="gf-kpi-sub">${pctVisible.toFixed(0)}% of ${radiusKm.toFixed(1)} km radius disc</div>
      </div>
      <div class="gf-kpi-tile">
        <div class="gf-kpi-label">Max Radius</div>
        <div class="gf-kpi-value mono">${radiusKm.toFixed(1)} km</div>
        <div class="gf-kpi-sub mono">EPSG:${res.utm_epsg}</div>
      </div>
    </div>

    ${
      p.answer
        ? `<section class="gf-answer-card"><p class="gf-answer-text">${escapeHtml(p.answer)}</p></section>`
        : ""
    }

    <div class="gf-split-layout">
      <div class="gf-stack">
        <div data-map-mount></div>
      </div>

      <section class="gf-card gf-stack">
        <div class="gf-section-title">
          <span>Observer & Radius Parameters</span>
          <span>Live DEM Viewshed</span>
        </div>
        <form data-viewshed-form>
          <div class="gf-form-grid">
            <div class="gf-field">
              <label for="vs-obs-h">Observer Ht (m)</label>
              <input id="vs-obs-h" name="observer_height_m" type="number" step="0.5" min="0.5" max="200" class="gf-input mono" value="${obs.observer_height_m}" />
            </div>
            <div class="gf-field">
              <label for="vs-tgt-h">Target Ht (m)</label>
              <input id="vs-tgt-h" name="target_height_m" type="number" step="0.5" min="0" max="200" class="gf-input mono" value="${obs.target_height_m}" />
            </div>
            <div class="gf-field">
              <label for="vs-rad">Radius (km)</label>
              <input id="vs-rad" name="max_distance_km" type="number" step="1" min="1" max="30" class="gf-input mono" value="${radiusKm.toFixed(1)}" />
            </div>
          </div>
          <div class="gf-actions">
            <button type="submit" class="gf-btn gf-btn-secondary" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Recalculate Viewshed</button>
            <button type="button" class="gf-btn gf-btn-primary" data-viewshed-render ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Render Viewshed PDF</button>
            ${
              hasStudioLink
                ? `<button type="button" class="gf-btn" data-action="open-studio">Open in Studio ↗</button>`
                : ""
            }
            ${renderHostConversationButtons("Ask Follow-up")}
          </div>
        </form>
      </section>
    </div>
  `;
}

/* ============================================================================
 * View 5: Route Card & Strip Atlas Planner (`route`)
 * ============================================================================ */

function renderRouteView(p: RouteViewPayload): string {
  const rm = p.route_metrics;
  const ruler = p.ruler_metrics;
  const strip = p.strip_plan;
  const waypoints = p.waypoints || rm?.waypoints || [];
  const totalKm =
    strip?.route_distance_km ??
    rm?.total_distance_km ??
    (ruler ? ruler.total_distance_m / 1000 : 0);
  const totalMi = rm?.total_distance_mi ?? totalKm * 0.621371;
  const sheetCount = strip?.sheets?.length ?? 0;
  const legCount = rm?.legs?.length ?? ruler?.segments?.length ?? 0;

  const hasStudioLink =
    state.caps.openLinks &&
    Boolean(resolveExternalUrl(p.share_url) || resolveExternalUrl(p.studio_url));

  const legsRows = rm?.legs?.length
    ? rm.legs
        .map(
          (l) => `
        <tr>
          <td class="mono">${escapeHtml(l.from_code)} → ${escapeHtml(l.to_code)}</td>
          <td class="mono">${l.leg_dist_km.toFixed(2)} km</td>
          <td class="mono">${l.cum_dist_km.toFixed(2)} km</td>
          <td class="mono">${l.true_az_deg.toFixed(1)}°</td>
          <td class="mono">${l.grid_az_deg.toFixed(1)}°</td>
          <td class="mono">${l.mag_az_deg.toFixed(1)}°</td>
          <td class="mono">${Math.round(l.mag_az_mils)}</td>
          <td class="mono">—</td>
        </tr>
      `
        )
        .join("")
    : ruler?.segments?.length
      ? ruler.segments
          .map((s) => {
            const elevTxt =
              s.elev_start_m != null && s.elev_end_m != null
                ? `${Math.round(s.elev_start_m)}→${Math.round(s.elev_end_m)}m${s.grade_pct != null ? ` (${s.grade_pct >= 0 ? "+" : ""}${s.grade_pct.toFixed(1)}%)` : ""}`
                : "—";
            return `
          <tr>
            <td class="mono">P${s.from_idx + 1} → P${s.to_idx + 1}</td>
            <td class="mono">${(s.distance_m / 1000).toFixed(2)} km</td>
            <td class="mono">—</td>
            <td class="mono">${s.bearing_true_deg.toFixed(1)}°</td>
            <td class="mono">${s.bearing_grid_deg.toFixed(1)}°</td>
            <td class="mono">${s.bearing_mag_deg.toFixed(1)}°</td>
            <td class="mono">${Math.round(s.bearing_mils)}</td>
            <td class="mono">${escapeHtml(elevTxt)}</td>
          </tr>
        `;
          })
          .join("")
      : "";

  return `
    ${renderHeader(
      p.title || (strip ? "Strip Atlas Corridor Plan" : "Tactical Route Card"),
      p.summary || `${totalKm.toFixed(2)} km (${totalMi.toFixed(2)} mi)${sheetCount ? ` · ${sheetCount} strip sheets` : ""}`,
      `
        <span class="gf-badge gf-badge-info mono">${totalKm.toFixed(2)} km</span>
        ${sheetCount ? `<span class="gf-badge gf-badge-marginal mono">${sheetCount} Sheets</span>` : ""}
      `
    )}
    ${renderToastBanner()}

    <div class="gf-kpi-strip">
      <div class="gf-kpi-tile gf-kpi-tile-cyan">
        <div class="gf-kpi-label">Total Distance</div>
        <div class="gf-kpi-value mono">${totalKm.toFixed(2)} km</div>
        <div class="gf-kpi-sub mono">${totalMi.toFixed(2)} miles</div>
      </div>
      <div class="gf-kpi-tile">
        <div class="gf-kpi-label">Route Legs</div>
        <div class="gf-kpi-value mono">${legCount} leg${legCount === 1 ? "" : "s"}</div>
        <div class="gf-kpi-sub">${waypoints.length} waypoint${waypoints.length === 1 ? "" : "s"}</div>
      </div>
      ${
        strip
          ? `
        <div class="gf-kpi-tile gf-kpi-tile-amber">
          <div class="gf-kpi-label">Strip Atlas</div>
          <div class="gf-kpi-value mono">${sheetCount} Sheet${sheetCount === 1 ? "" : "s"}</div>
          <div class="gf-kpi-sub mono">${strip.gzd ? `GZD ${escapeHtml(strip.gzd)} · ` : ""}${strip.utm_epsg ? `EPSG:${strip.utm_epsg}` : "True-Scale UTM"}</div>
        </div>
      `
          : ""
      }
    </div>

    ${
      p.answer
        ? `<section class="gf-answer-card"><p class="gf-answer-text">${escapeHtml(p.answer)}</p></section>`
        : ""
    }

    <div data-map-mount></div>

    ${
      legsRows
        ? `
      <section class="gf-card">
        <div class="gf-section-title">
          <span>Tactical Route Card</span>
          <span>True · Grid · Magnetic · NATO Mils</span>
        </div>
        <div class="gf-table-wrap">
          <table class="gf-table">
            <thead>
              <tr>
                <th>Leg</th>
                <th>Dist</th>
                <th>Cum</th>
                <th>True</th>
                <th>Grid</th>
                <th>Mag</th>
                <th>Mils</th>
                <th>Elev / Grade</th>
              </tr>
            </thead>
            <tbody>${legsRows}</tbody>
          </table>
        </div>
      </section>
    `
        : ""
    }

    ${
      strip && strip.sheets.length > 0
        ? `
      <section class="gf-card">
        <div class="gf-section-title">
          <span>Corridor Strip Sheets (${strip.sheets.length})</span>
          <span>Ordered start → finish</span>
        </div>
        <div class="gf-pill-row" style="margin-top:0">
          ${strip.sheets
            .map(
              (s) =>
                `<span class="gf-chip gf-chip-amber mono">${escapeHtml(s.sheet_id)}${s.center_mgrs_10m ? ` · ${escapeHtml(s.center_mgrs_10m)}` : ""}</span>`
            )
            .join("")}
        </div>
      </section>
    `
        : ""
    }

    <div class="gf-actions" style="margin-top:0">
      <button type="button" class="gf-btn gf-btn-primary" data-route-render ${!state.caps.serverTools || state.busy ? "disabled" : ""}>${strip ? "Render Strip Atlas PDF" : "Render Route Card PDF"}</button>
      ${
        hasStudioLink
          ? `<button type="button" class="gf-btn" data-action="open-studio">Open in Studio ↗</button>`
          : ""
      }
      ${renderHostConversationButtons("Ask Follow-up")}
    </div>
  `;
}

/* ============================================================================
 * View 6: Interactive Printable Sheet Viewer (`render`)
 * ============================================================================ */

function renderJobView(p: RenderViewPayload): string {
  const j = p.job;
  const pageCount = Math.max(1, p.page_count || p.pages?.length || 1);
  const statusBadgeClass =
    j.status === "succeeded"
      ? "gf-badge-clear"
      : j.status === "failed" || j.status === "cancelled"
        ? "gf-badge-obstructed"
        : "gf-badge-marginal";
  const statusLabel =
    j.status === "succeeded"
      ? "Ready"
      : j.status === "running"
        ? `Rendering ${j.progress_pct}%`
        : j.status === "queued"
          ? "Queued"
          : j.status === "failed"
            ? "Failed"
            : "Cancelled";

  const cacheKey = `${j.job_id}:${state.activePage}`;
  const cachedPage = state.pageCache.get(cacheKey);
  const imgSrc = cachedPage
    ? `data:${cachedPage.mime_type || "image/png"};base64,${cachedPage.data_base64}`
    : null;
  const zoomWidth =
    state.pageZoom === "100"
      ? "100%"
      : state.pageZoom === "150"
        ? "150%"
        : state.pageZoom === "200"
          ? "200%"
          : "";

  const artifacts = j.artifacts ?? [];
  const hasStudioLink =
    state.caps.openLinks &&
    Boolean(resolveExternalUrl(p.share_url) || resolveExternalUrl(p.studio_url));

  let bodyHtml = "";
  if (j.status === "queued" || j.status === "running") {
    const pct = Math.max(4, Math.min(100, j.progress_pct || 0));
    bodyHtml = `
      <section class="gf-card">
        <div class="gf-section-title">
          <span>PyQGIS True-Scale Print Engine</span>
          <span class="mono">${pct}%</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="gf-spinner" aria-hidden="true"></div>
          <div>
            <div style="font-weight:700;font-size:13px;">${escapeHtml(j.stage || "Preparing topographic layers…")}</div>
            <div style="font-size:11.5px;color:var(--gf-text-secondary);">Live polling job <span class="mono">${escapeHtml(j.job_id)}</span> every 2s…</div>
          </div>
        </div>
        <div class="gf-progress-track" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
          <div class="gf-progress-fill" style="width:${pct}%"></div>
        </div>
        <div class="gf-actions">
          <button type="button" class="gf-btn gf-btn-sm" data-render-refresh ${!state.caps.serverTools ? "disabled" : ""}>Refresh Now</button>
        </div>
      </section>
    `;
  } else if (j.status === "failed" || j.status === "cancelled") {
    bodyHtml = `
      <section class="gf-card" style="border-left:3px solid var(--gf-accent-red);">
        <div class="gf-section-title"><span>Render ${escapeHtml(j.status)}</span></div>
        <p class="gf-answer-text">${escapeHtml(j.error || "The print render did not complete.")}</p>
        <div class="gf-actions">
          <button type="button" class="gf-btn gf-btn-primary" data-render-retry ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Retry Render</button>
        </div>
      </section>
    `;
  } else {
    const pagePills = Array.from({ length: Math.min(pageCount, 12) }, (_, idx) => {
      const pg = idx + 1;
      return `<button type="button" class="gf-pill-btn ${pg === state.activePage ? "active" : ""}" data-sheet-page="${pg}">P${pg}</button>`;
    }).join("");

    const zoomPills: Array<{ id: AppRuntimeState["pageZoom"]; label: string }> = [
      { id: "fit", label: "Fit" },
      { id: "100", label: "100%" },
      { id: "150", label: "150%" },
      { id: "200", label: "200%" },
    ];

    bodyHtml = `
      <section class="gf-card">
        <div class="gf-sheet-toolbar">
          <div class="gf-sheet-group" role="group" aria-label="Sheet pages">
            <button type="button" class="gf-btn gf-btn-sm" data-sheet-page="${Math.max(1, state.activePage - 1)}" ${state.activePage <= 1 ? "disabled" : ""}>‹ Prev</button>
            ${pageCount > 1 ? pagePills : `<span class="gf-chip mono">Page 1 of 1</span>`}
            <button type="button" class="gf-btn gf-btn-sm" data-sheet-page="${Math.min(pageCount, state.activePage + 1)}" ${state.activePage >= pageCount ? "disabled" : ""}>Next ›</button>
          </div>
          <div class="gf-sheet-group" role="group" aria-label="Zoom level">
            ${zoomPills
              .map(
                (z) =>
                  `<button type="button" class="gf-pill-btn ${state.pageZoom === z.id ? "active" : ""}" data-sheet-zoom="${z.id}">${z.label}</button>`
              )
              .join("")}
          </div>
        </div>

        <div class="gf-sheet-viewport">
          ${
            state.pageLoading
              ? `<div class="gf-loading-card"><div class="gf-spinner"></div><span style="color:var(--gf-text-secondary)">Rasterizing page ${state.activePage} preview…</span></div>`
              : state.pageError
                ? `<div class="gf-empty-panel">${escapeHtml(state.pageError)}</div>`
                : imgSrc
                  ? `<img class="gf-sheet-img ${state.pageZoom === "fit" ? "zoom-fit" : ""}" ${zoomWidth ? `style="width:${zoomWidth}"` : ""} src="${imgSrc}" alt="Printable sheet page ${state.activePage} of ${pageCount}" />`
                  : `<div class="gf-empty-panel">Loading sheet preview…</div>`
          }
        </div>

        ${
          artifacts.length > 0
            ? `
          <div class="gf-section-title" style="margin-top:10px;">
            <span>Download Print & Device Artifacts</span>
            <span>${j.elapsed_seconds != null ? `Rendered in ${j.elapsed_seconds.toFixed(1)}s` : "Signed download links"}</span>
          </div>
          <div class="gf-artifact-row">
            ${artifacts
              .map(
                (a, idx) => `
              <button type="button" class="gf-artifact-pill" data-download-idx="${idx}" ${!state.caps.openLinks && !state.demoMode ? "disabled" : ""}>
                <span>↓ ${escapeHtml(formatArtifactLabel(a.kind))}</span>
                <span class="gf-artifact-size mono">${escapeHtml(formatBytes(a.size_bytes))}</span>
              </button>
            `
              )
              .join("")}
          </div>
        `
            : ""
        }

        <div class="gf-actions">
          ${
            hasStudioLink
              ? `<button type="button" class="gf-btn" data-action="open-studio">Open in Studio ↗</button>`
              : ""
          }
          ${renderHostConversationButtons("Inspect in Chat")}
        </div>
      </section>
    `;
  }

  return `
    ${renderHeader(
      p.title || j.title || `Print Sheet · ${j.center_mgrs || j.center}`,
      `${formatScale(j.scale)} · ${j.paper.toUpperCase()} ${j.orientation} · ${j.color_mode}${j.grid_cols && j.grid_rows && (j.grid_cols > 1 || j.grid_rows > 1) ? ` · ${j.grid_cols}×${j.grid_rows} Atlas` : ""}`,
      `<span class="gf-badge ${statusBadgeClass}">${escapeHtml(statusLabel)}</span>`
    )}
    ${renderToastBanner()}
    ${
      p.answer
        ? `<section class="gf-answer-card"><p class="gf-answer-text">${escapeHtml(p.answer)}</p></section>`
        : ""
    }
    ${bodyHtml}
  `;
}

/* ============================================================================
 * View 7: Place Search / Geocode Results (`geocode`)
 * ============================================================================ */

function renderGeocodeView(p: GeocodeViewPayload): string {
  const results = p.results ?? [];
  return `
    ${renderHeader(
      p.title || "Place Search Results",
      p.summary || (p.query ? `Matches for "${p.query}" — select a location to open its tactical briefing` : "Select a location"),
      `<span class="gf-badge gf-badge-neutral">${results.length} Result${results.length === 1 ? "" : "s"}</span>`
    )}
    ${renderToastBanner()}

    <section class="gf-card">
      <form class="gf-search-row" data-geocode-search-form>
        <input type="text" class="gf-input" name="query" value="${escapeHtml(p.query || "")}" placeholder="Search mountain, park, town, or landmark…" />
        <button type="submit" class="gf-btn gf-btn-secondary" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>Search</button>
      </form>
    </section>

    <section class="gf-card">
      <div class="gf-section-title">
        <span>Click any match to launch Location Briefing</span>
        <span>Lat / Lon</span>
      </div>
      ${
        results.length === 0
          ? `<div class="gf-empty-panel">No matching places found. Try a broader mountain, town, or park name.</div>`
          : `
        <div class="gf-list">
          ${results
            .map(
              (r, idx) => `
            <button type="button" class="gf-list-item gf-list-item-btn" data-geocode-idx="${idx}" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>
              <div class="gf-list-main">
                <div class="gf-list-title">${escapeHtml(r.short_name || r.display_name)}</div>
                <div class="gf-list-sub">${escapeHtml(r.display_name)}</div>
              </div>
              <span class="gf-chip gf-chip-cyan mono">${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}</span>
            </button>
          `
            )
            .join("")}
        </div>
      `
      }
    </section>
  `;
}

/* ============================================================================
 * View 8: Offline Regional Packs & Catalog (`packs`)
 * ============================================================================ */

function renderPacksView(p: PacksViewPayload): string {
  const packs = p.packs ?? [];
  const allRegions = p.regions ?? [];
  const q = state.packsSearchQuery.trim().toLowerCase();
  const filteredRegions = allRegions.filter((r) => {
    if (state.packsRegionFilter !== "ALL") {
      if (mapPackGroupToFilter(r.group) !== state.packsRegionFilter) return false;
    }
    if (q) {
      const hay = `${r.name} ${r.region_id} ${r.description || ""} ${r.group}`.toLowerCase();
      return hay.includes(q);
    }
    return true;
  });

  const filterTabs: Array<{ id: AppRuntimeState["packsRegionFilter"]; label: string }> = [
    { id: "ALL", label: `All (${allRegions.length})` },
    { id: "AU", label: "AU" },
    { id: "NZ", label: "NZ" },
    { id: "NA", label: "NA" },
  ];

  return `
    ${renderHeader(
      p.title || "Air-Gapped Offline Regional Packs",
      p.summary || "Pre-packaged regional DEM, OSM vectors, trails & offline basemaps",
      `<span class="gf-badge gf-badge-clear">${packs.length} Installed</span>`
    )}
    ${renderToastBanner()}

    <section class="gf-card">
      <div class="gf-section-title">
        <span>Installed Local Packs</span>
        <span>Ready for zero-internet rendering</span>
      </div>
      ${
        packs.length === 0
          ? `<div class="gf-empty-panel">No local offline packs installed yet. Build any curated region below in GridForge Studio or CLI.</div>`
          : `
        <div class="gf-list">
          ${packs
            .map(
              (pk, idx) => `
            <div class="gf-list-item">
              <div class="gf-list-main">
                <div class="gf-list-title">${escapeHtml(pk.name)}</div>
                <div class="gf-list-sub mono">${escapeHtml(pk.region)} · UTM ${escapeHtml((pk.utm_zones || []).join(", ") || "—")} · ${escapeHtml(formatBytes(pk.size_bytes))}</div>
              </div>
              <div style="display:flex;align-items:center;gap:5px;">
                ${pk.has_basemap ? `<span class="gf-chip gf-chip-emerald">Basemap</span>` : ""}
                ${
                  pk.bbox_wgs84 && state.caps.serverTools
                    ? `<button type="button" class="gf-btn gf-btn-sm gf-btn-secondary" data-pack-brief-idx="${idx}" ${state.busy ? "disabled" : ""}>Brief</button>`
                    : ""
                }
              </div>
            </div>
          `
            )
            .join("")}
        </div>
      `
      }
    </section>

    <section class="gf-card">
      <div class="gf-section-title">
        <span>Curated Regional Pack Catalog</span>
        <span>${filteredRegions.length} shown</span>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
        <div class="gf-band-pills" style="margin-bottom:0">
          ${filterTabs
            .map(
              (t) =>
                `<button type="button" class="gf-pill-btn ${state.packsRegionFilter === t.id ? "active" : ""}" data-pack-filter="${t.id}">${escapeHtml(t.label)}</button>`
            )
            .join("")}
        </div>
        <input type="text" class="gf-input" style="max-width:210px;" data-pack-search placeholder="Filter regions…" value="${escapeHtml(state.packsSearchQuery)}" />
      </div>
      ${
        filteredRegions.length === 0
          ? `<div class="gf-empty-panel">No regions match this filter.</div>`
          : `
        <div class="gf-preset-grid">
          ${filteredRegions
            .map(
              (r) => `
            <button type="button" class="gf-preset-card" data-region-id="${escapeHtml(r.region_id)}" ${!state.caps.serverTools || state.busy ? "disabled" : ""}>
              <span class="gf-preset-name">${escapeHtml(r.name)}</span>
              <span class="gf-preset-meta">
                <span>${escapeHtml(mapPackGroupToFilter(r.group))} · ${escapeHtml((r.utm_zones || []).slice(0, 2).join("/") || "UTM")}</span>
                <span class="mono">${escapeHtml(r.estimated_size_hint || "")}</span>
              </span>
            </button>
          `
            )
            .join("")}
        </div>
      `
      }
    </section>
  `;
}

/* ============================================================================
 * Master Render & Event Wiring
 * ============================================================================ */

function renderLoadingShell(message: string, detail = ""): string {
  return `
    ${renderHeader("GridForge", "Interactive Topographic Map & RF Studio")}
    <section class="gf-card gf-loading-card">
      <div class="gf-spinner" aria-hidden="true"></div>
      <div style="font-weight:700;font-size:13px;">${escapeHtml(message)}</div>
      ${detail ? `<div class="mono" style="font-size:11px;color:var(--gf-text-secondary);">${escapeHtml(detail)}</div>` : ""}
    </section>
  `;
}

function renderCurrentView(): void {
  const root = getRootEl();
  if (!root) return;
  cleanupInteractiveMounts();

  const p = state.payload;
  if (!p) {
    root.innerHTML = renderLoadingShell(
      state.busyLabel || "Connecting to GridForge MCP…",
      "Awaiting tool data from host"
    );
    return;
  }

  switch (p.view) {
    case "studio":
      root.innerHTML = renderStudioView(p);
      bindStudioEvents(root, p);
      break;
    case "briefing":
      root.innerHTML = renderBriefingView(p);
      bindBriefingEvents(root, p);
      break;
    case "rf_link":
      root.innerHTML = renderRFLinkView(p);
      bindRFLinkEvents(root, p);
      break;
    case "viewshed":
      root.innerHTML = renderViewshedView(p);
      bindViewshedEvents(root, p);
      break;
    case "route":
      root.innerHTML = renderRouteView(p);
      bindRouteEvents(root, p);
      break;
    case "render":
      root.innerHTML = renderJobView(p);
      bindRenderEvents(root, p);
      break;
    case "geocode":
      root.innerHTML = renderGeocodeView(p);
      bindGeocodeEvents(root, p);
      break;
    case "packs":
      root.innerHTML = renderPacksView(p);
      bindPacksEvents(root, p);
      break;
  }

  root.querySelector("[data-dismiss-toast]")?.addEventListener("click", () => {
    state.toast = null;
    renderCurrentView();
  });
}

function readAtlasValue(atlasStr: string): { grid_cols: number; grid_rows: number } {
  const found = ATLAS_PRESETS.find((a) => a.id === atlasStr);
  return found ? { grid_cols: found.cols, grid_rows: found.rows } : { grid_cols: 1, grid_rows: 1 };
}

function bindStudioEvents(root: HTMLElement, p: StudioViewPayload): void {
  const presets = p.presets && p.presets.length > 0 ? p.presets : DEFAULT_STUDIO_PRESETS;

  root.querySelector("[data-action='open-studio']")?.addEventListener("click", () => {
    void openExternalUrl(p.share_url || p.public_url, "GridForge Studio");
  });

  root.querySelector("[data-action='browse-packs']")?.addEventListener("click", () => {
    void invokeViewTool("list_packs", {}, "Loading offline packs…");
  });

  const searchForm = root.querySelector<HTMLFormElement>("[data-studio-search-form]");
  searchForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = new FormData(searchForm).get("query")?.toString().trim() || "";
    if (!q) return;
    void invokeViewTool("geocode_place", { query: q }, `Searching "${q}"…`);
  });

  root.querySelector("[data-studio-brief-direct]")?.addEventListener("click", () => {
    if (!searchForm) return;
    const q = new FormData(searchForm).get("query")?.toString().trim() || "";
    if (!q) return;
    void invokeViewTool("answer_location_briefing", { location: q }, `Briefing "${q}"…`);
  });

  root.querySelectorAll<HTMLButtonElement>("[data-preset-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const pr = presets.find((x) => x.id === btn.dataset.presetId);
      if (!pr) return;
      void invokeViewTool(
        "answer_location_briefing",
        {
          location: pr.center,
          question: `Tactical navigation briefing for ${pr.name}`,
          scale: pr.scale,
          paper: pr.paper,
          color_mode: pr.color_mode,
        },
        `Loading ${pr.name}…`
      );
    });
  });

  const buildForm = root.querySelector<HTMLFormElement>("[data-studio-build-form]");
  const readBuildArgs = () => {
    if (!buildForm) return null;
    const fd = new FormData(buildForm);
    const center = fd.get("center")?.toString().trim() || "";
    if (!center) return null;
    const atlas = readAtlasValue(fd.get("atlas")?.toString() || "1x1");
    return {
      center,
      scale: Number(fd.get("scale") || 25000),
      color_mode: (fd.get("color_mode")?.toString() || "color") as ColorMode,
      paper: (fd.get("paper")?.toString() || "a4") as PaperSize,
      orientation: (fd.get("orientation")?.toString() || "portrait") as Orientation,
      grid_cols: atlas.grid_cols,
      grid_rows: atlas.grid_rows,
    };
  };

  buildForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const args = readBuildArgs();
    if (!args) return;
    void invokeViewTool("preview_footprint", args, "Computing true-scale footprint…");
  });

  root.querySelector("[data-studio-render]")?.addEventListener("click", () => {
    const args = readBuildArgs();
    if (!args) return;
    void invokeViewTool(
      "render_map",
      { ...args, wait: false },
      "Submitting printable PDF render…"
    );
  });
}

function bindBriefingEvents(root: HTMLElement, p: BriefingViewPayload): void {
  const mapMount = root.querySelector<HTMLElement>("[data-map-mount]");
  const form = root.querySelector<HTMLFormElement>("[data-briefing-form]");

  const readControls = () => {
    const fd = form ? new FormData(form) : null;
    const atlas = readAtlasValue(fd?.get("atlas")?.toString() || `${p.sheet.grid_cols || 1}x${p.sheet.grid_rows || 1}`);
    return {
      scale: Number(fd?.get("scale") || p.sheet.scale || 25000),
      color_mode: (fd?.get("color_mode")?.toString() || p.sheet.color_mode || "color") as ColorMode,
      paper: (fd?.get("paper")?.toString() || p.sheet.paper || "a4") as PaperSize,
      orientation: (fd?.get("orientation")?.toString() || p.sheet.orientation || "portrait") as Orientation,
      dpi: Number(fd?.get("dpi") || p.sheet.dpi || 300),
      grid_cols: atlas.grid_cols,
      grid_rows: atlas.grid_rows,
    };
  };

  if (mapMount) {
    activeMap = mountMapCanvas(
      mapMount,
      {
        center: { lat: p.location.lat, lon: p.location.lon },
        footprintGeoJson: p.footprint_geojson,
        gridGeoJson: p.grid_geojson,
        waypoints: p.waypoints,
      },
      {
        onCenterClick: state.caps.serverTools
          ? (lat, lon) => {
              const ctrl = readControls();
              void invokeViewTool(
                "preview_footprint",
                {
                  center: `${lat.toFixed(5)}, ${lon.toFixed(5)}`,
                  scale: ctrl.scale,
                  paper: ctrl.paper,
                  orientation: ctrl.orientation,
                  color_mode: ctrl.color_mode,
                  grid_cols: ctrl.grid_cols,
                  grid_rows: ctrl.grid_rows,
                },
                "Recentering footprint…"
              );
            }
          : undefined,
      }
    );
  }

  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const ctrl = readControls();
    void invokeViewTool(
      "preview_footprint",
      {
        center: `${p.location.lat.toFixed(5)}, ${p.location.lon.toFixed(5)}`,
        scale: ctrl.scale,
        paper: ctrl.paper,
        orientation: ctrl.orientation,
        color_mode: ctrl.color_mode,
        grid_cols: ctrl.grid_cols,
        grid_rows: ctrl.grid_rows,
      },
      "Updating true-scale footprint…"
    );
  });

  root.querySelector("[data-briefing-render]")?.addEventListener("click", () => {
    const ctrl = readControls();
    void invokeViewTool(
      "render_map",
      {
        center: `${p.location.lat.toFixed(5)}, ${p.location.lon.toFixed(5)}`,
        title: p.location.place_name || p.title || undefined,
        scale: ctrl.scale,
        paper: ctrl.paper,
        orientation: ctrl.orientation,
        color_mode: ctrl.color_mode,
        dpi: ctrl.dpi,
        grid_cols: ctrl.grid_cols,
        grid_rows: ctrl.grid_rows,
        waypoints: p.waypoints && p.waypoints.length > 0 ? p.waypoints : undefined,
        wait: false,
      },
      "Queueing printable PDF render…"
    );
  });

  root.querySelector("[data-action='open-studio']")?.addEventListener("click", () => {
    const ctrl = readControls();
    void openInStudioAction(p.studio_url, p.share_url, {
      name: p.location.place_name || `Briefing ${p.location.mgrs_10m}`,
      state: {
        center: `${p.location.lat.toFixed(5)}, ${p.location.lon.toFixed(5)}`,
        scale: ctrl.scale,
        paper: ctrl.paper,
        orientation: ctrl.orientation,
        color_mode: ctrl.color_mode,
        grid_cols: ctrl.grid_cols,
        grid_rows: ctrl.grid_rows,
      },
    });
  });

  root.querySelector("[data-action='attach-context']")?.addEventListener("click", () => {
    void attachPayloadToChat();
  });

  root.querySelector("[data-action='ask-followup']")?.addEventListener("click", () => {
    void askFollowUpInChat(
      `Using this ${p.location.place_name || p.location.mgrs_10m} briefing (${formatScale(p.sheet.scale)}, G-M ${formatSignedDeg(p.declination.gm_angle_deg)}), what terrain hazards, contour spacing, or RF comms checks should I plan before printing?`
    );
  });
}

function bindRFLinkEvents(root: HTMLElement, p: RFLinkViewPayload): void {
  const chartMount = root.querySelector<HTMLElement>("[data-rf-mount]");
  if (chartMount) {
    activeChartCleanup = mountRFChart(chartMount, p.profile);
  }

  const form = root.querySelector<HTMLFormElement>("[data-rf-form]");
  const freqInput = root.querySelector<HTMLInputElement>("#rf-freq");

  root.querySelectorAll<HTMLButtonElement>("[data-rf-band]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (freqInput && btn.dataset.rfBand) {
        freqInput.value = btn.dataset.rfBand;
        root.querySelectorAll("[data-rf-band]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
      }
    });
  });

  const readRFArgs = () => {
    const fd = form ? new FormData(form) : null;
    return {
      from_lat: p.request.from_lat,
      from_lon: p.request.from_lon,
      to_lat: p.request.to_lat,
      to_lon: p.request.to_lon,
      from_label: p.request.from_label || p.profile.from_label || "TX",
      to_label: p.request.to_label || p.profile.to_label || "RX",
      freq_mhz: Number(fd?.get("freq_mhz") || p.request.freq_mhz || 915),
      from_height_m: Number(fd?.get("from_height_m") ?? p.request.from_height_m ?? 2),
      to_height_m: Number(fd?.get("to_height_m") ?? p.request.to_height_m ?? 2),
      dem_source: p.request.dem_source || undefined,
    };
  };

  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    void invokeViewTool("analyze_rf_link", readRFArgs(), "Recalculating Fresnel profile…");
  });

  root.querySelector("[data-rf-render]")?.addEventListener("click", () => {
    const rf = readRFArgs();
    const midLat = (rf.from_lat + rf.to_lat) / 2;
    const midLon = (rf.from_lon + rf.to_lon) / 2;
    const autoScale = p.profile.distance_km > 8 ? 50000 : 25000;
    void invokeViewTool(
      "render_map",
      {
        center: `${midLat.toFixed(5)}, ${midLon.toFixed(5)}`,
        title: `RF Link · ${rf.from_label} to ${rf.to_label}`,
        scale: autoScale,
        paper: "a4",
        orientation: "landscape",
        color_mode: "color",
        include_rf_link_card: true,
        rf_links: [{ ...rf, k_factor: 1.333 }],
        wait: false,
      },
      "Queueing RF Link Card PDF…"
    );
  });

  root.querySelector("[data-action='open-studio']")?.addEventListener("click", () => {
    void openExternalUrl(p.share_url || p.studio_url, "GridForge Studio");
  });

  root.querySelector("[data-action='attach-context']")?.addEventListener("click", () => {
    void attachPayloadToChat();
  });

  root.querySelector("[data-action='ask-followup']")?.addEventListener("click", () => {
    void askFollowUpInChat(
      `This ${p.profile.freq_mhz} MHz link (${p.profile.from_label} → ${p.profile.to_label}, ${p.profile.distance_km.toFixed(2)} km) is ${p.profile.status} with min clearance ${p.profile.min_clearance_m.toFixed(1)}m (${p.profile.min_fresnel_pct.toFixed(0)}% F1) and ${p.profile.fspl_db.toFixed(1)} dB FSPL. What antenna gain, mast height, or relay placement do you recommend?`
    );
  });
}

function bindViewshedEvents(root: HTMLElement, p: ViewshedViewPayload): void {
  const mapMount = root.querySelector<HTMLElement>("[data-map-mount]");
  if (mapMount) {
    activeMap = mountMapCanvas(mapMount, {
      center: { lat: p.observer.lat, lon: p.observer.lon },
      viewshedGeoJson: p.result.viewshed_geojson,
      rangeRingGeoJson: p.result.range_ring_geojson,
      observer: p.observer,
      hintLabel: "Emerald = visible terrain · Drag to pan · Scroll to zoom",
    });
  }

  const form = root.querySelector<HTMLFormElement>("[data-viewshed-form]");
  const readObsArgs = () => {
    const fd = form ? new FormData(form) : null;
    const radKm = Number(fd?.get("max_distance_km") || p.observer.max_distance_m / 1000 || 5);
    return {
      label: p.observer.label || "OBS1",
      lat: p.observer.lat,
      lon: p.observer.lon,
      observer_height_m: Number(fd?.get("observer_height_m") ?? p.observer.observer_height_m ?? 2),
      target_height_m: Number(fd?.get("target_height_m") ?? p.observer.target_height_m ?? 2),
      max_distance_m: Math.round(radKm * 1000),
    };
  };

  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    void invokeViewTool("compute_viewshed", readObsArgs(), "Computing terrain viewshed…");
  });

  root.querySelector("[data-viewshed-render]")?.addEventListener("click", () => {
    const obs = readObsArgs();
    const autoScale = obs.max_distance_m > 6000 ? 50000 : 25000;
    void invokeViewTool(
      "render_map",
      {
        center: `${obs.lat.toFixed(5)}, ${obs.lon.toFixed(5)}`,
        title: `Viewshed · ${obs.label}`,
        scale: autoScale,
        paper: "a4",
        orientation: "portrait",
        color_mode: "color",
        viewsheds: [obs],
        wait: false,
      },
      "Queueing Viewshed PDF render…"
    );
  });

  root.querySelector("[data-action='open-studio']")?.addEventListener("click", () => {
    void openExternalUrl(p.share_url || p.studio_url, "GridForge Studio");
  });

  root.querySelector("[data-action='attach-context']")?.addEventListener("click", () => {
    void attachPayloadToChat();
  });

  root.querySelector("[data-action='ask-followup']")?.addEventListener("click", () => {
    void askFollowUpInChat(
      `Analyze this viewshed from ${p.observer.label} (${p.result.observer_mgrs_10m}, ${p.result.visible_area_km2.toFixed(2)} km² visible out to ${(p.result.max_distance_m / 1000).toFixed(1)} km). Where are the likely shadow zones or better repeater vantage points?`
    );
  });
}

function bindRouteEvents(root: HTMLElement, p: RouteViewPayload): void {
  const mapMount = root.querySelector<HTMLElement>("[data-map-mount]");
  const waypoints = p.waypoints || p.route_metrics?.waypoints || [];
  if (mapMount) {
    activeMap = mountMapCanvas(mapMount, {
      footprintGeoJson: p.footprint_geojson || p.strip_plan?.footprint_geojson || null,
      gridGeoJson: p.grid_geojson || p.strip_plan?.grid_geojson || null,
      routeGeoJson: p.strip_plan?.route_geojson || null,
      waypoints,
      connectWaypoints: !p.strip_plan?.route_geojson,
      hintLabel: "Drag to pan · Scroll to zoom · Fit to reset",
    });
  }

  root.querySelector("[data-route-render]")?.addEventListener("click", () => {
    const firstWp = waypoints[0];
    const firstSheet = p.strip_plan?.sheets?.[0];
    const center = firstSheet
      ? `${firstSheet.center_lat.toFixed(5)}, ${firstSheet.center_lon.toFixed(5)}`
      : firstWp
        ? `${firstWp.lat.toFixed(5)}, ${firstWp.lon.toFixed(5)}`
        : "-33.7152, 150.3119";
    void invokeViewTool(
      "render_map",
      {
        center,
        title: p.title || (p.strip_plan ? "Strip Atlas" : "Tactical Route Card"),
        scale: 25000,
        paper: "a4",
        orientation: "portrait",
        color_mode: "color",
        waypoints: waypoints.length > 0 ? waypoints : undefined,
        strip_sheets: p.strip_plan?.sheets?.length ? p.strip_plan.sheets : undefined,
        include_route_card: true,
        include_atlas_index: Boolean(p.strip_plan?.sheets?.length),
        wait: false,
      },
      "Queueing Route / Strip Atlas PDF…"
    );
  });

  root.querySelector("[data-action='open-studio']")?.addEventListener("click", () => {
    void openExternalUrl(p.share_url || p.studio_url, "GridForge Studio");
  });

  root.querySelector("[data-action='attach-context']")?.addEventListener("click", () => {
    void attachPayloadToChat();
  });

  root.querySelector("[data-action='ask-followup']")?.addEventListener("click", () => {
    void askFollowUpInChat(
      `Review this tactical route card and suggest pacing checkpoints, bailout bearings, or supplementary pages to include when printing.`
    );
  });
}

function bindRenderEvents(root: HTMLElement, p: RenderViewPayload): void {
  const j = p.job;
  const pageCount = Math.max(1, p.page_count || p.pages?.length || 1);

  root.querySelector("[data-render-refresh]")?.addEventListener("click", () => {
    void invokeViewTool("get_job_status", { job_id: j.job_id }, "Refreshing job status…");
  });

  root.querySelector("[data-render-retry]")?.addEventListener("click", () => {
    void invokeViewTool(
      "render_map",
      {
        center: j.center,
        title: j.title || undefined,
        scale: j.scale,
        paper: j.paper,
        orientation: j.orientation,
        color_mode: j.color_mode,
        grid_cols: j.grid_cols || 1,
        grid_rows: j.grid_rows || 1,
        wait: false,
      },
      "Retrying PDF render…"
    );
  });

  root.querySelectorAll<HTMLButtonElement>("[data-sheet-page]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = Math.max(1, Math.min(pageCount, Number(btn.dataset.sheetPage || 1)));
      if (next === state.activePage) return;
      state.activePage = next;
      void ensureRenderPageLoaded(j.job_id, next);
    });
  });

  root.querySelectorAll<HTMLButtonElement>("[data-sheet-zoom]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const z = btn.dataset.sheetZoom as AppRuntimeState["pageZoom"] | undefined;
      if (!z) return;
      state.pageZoom = z;
      renderCurrentView();
    });
  });

  root.querySelectorAll<HTMLButtonElement>("[data-download-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.downloadIdx);
      const art = j.artifacts?.[idx];
      if (!art) return;
      void openExternalUrl(
        art.signed_download_url || art.download_url,
        art.filename || formatArtifactLabel(art.kind)
      );
    });
  });

  root.querySelector("[data-action='open-studio']")?.addEventListener("click", () => {
    void openExternalUrl(p.share_url || p.studio_url, "GridForge Studio");
  });

  root.querySelector("[data-action='attach-context']")?.addEventListener("click", () => {
    void attachPayloadToChat();
  });

  root.querySelector("[data-action='ask-followup']")?.addEventListener("click", () => {
    void askFollowUpInChat(
      `Inspect page ${state.activePage} of my rendered map (${j.title || j.job_id}) and verify the print framing, collar telemetry, and scale settings before I print it.`
    );
  });
}

function bindGeocodeEvents(root: HTMLElement, p: GeocodeViewPayload): void {
  const form = root.querySelector<HTMLFormElement>("[data-geocode-search-form]");
  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = new FormData(form).get("query")?.toString().trim() || "";
    if (!q) return;
    void invokeViewTool("geocode_place", { query: q }, `Searching "${q}"…`);
  });

  root.querySelectorAll<HTMLButtonElement>("[data-geocode-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.geocodeIdx);
      const item = p.results?.[idx];
      if (!item) return;
      void invokeViewTool(
        "answer_location_briefing",
        {
          location: `${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}`,
          question: `Navigation briefing for ${item.short_name || item.display_name}`,
        },
        `Loading ${item.short_name || "location"}…`
      );
    });
  });
}

function bindPacksEvents(root: HTMLElement, p: PacksViewPayload): void {
  root.querySelectorAll<HTMLButtonElement>("[data-pack-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const f = btn.dataset.packFilter as AppRuntimeState["packsRegionFilter"] | undefined;
      if (!f) return;
      state.packsRegionFilter = f;
      renderCurrentView();
    });
  });

  const searchInput = root.querySelector<HTMLInputElement>("[data-pack-search]");
  searchInput?.addEventListener("input", () => {
    state.packsSearchQuery = searchInput.value;
    const pos = searchInput.selectionStart ?? searchInput.value.length;
    renderCurrentView();
    const nextInput = document.querySelector<HTMLInputElement>("[data-pack-search]");
    if (nextInput) {
      nextInput.focus();
      nextInput.setSelectionRange(pos, pos);
    }
  });

  root.querySelectorAll<HTMLButtonElement>("[data-pack-brief-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const pk = p.packs?.[Number(btn.dataset.packBriefIdx)];
      if (!pk?.bbox_wgs84) return;
      const lat = (pk.bbox_wgs84.min_y + pk.bbox_wgs84.max_y) / 2;
      const lon = (pk.bbox_wgs84.min_x + pk.bbox_wgs84.max_x) / 2;
      void invokeViewTool(
        "answer_location_briefing",
        { location: `${lat.toFixed(5)}, ${lon.toFixed(5)}`, question: `Brief offline pack ${pk.name}` },
        `Briefing ${pk.name}…`
      );
    });
  });

  root.querySelectorAll<HTMLButtonElement>("[data-region-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const reg = p.regions?.find((r) => r.region_id === btn.dataset.regionId);
      if (!reg) return;
      const loc = reg.bbox_wgs84
        ? `${((reg.bbox_wgs84.min_y + reg.bbox_wgs84.max_y) / 2).toFixed(5)}, ${((reg.bbox_wgs84.min_x + reg.bbox_wgs84.max_x) / 2).toFixed(5)}`
        : reg.name;
      void invokeViewTool(
        "answer_location_briefing",
        { location: loc, question: `Brief region ${reg.name}` },
        `Briefing ${reg.name}…`
      );
    });
  });
}

/* ============================================================================
 * Standalone Browser Preview / Demo Mode (`#demo=briefing|rf_link|render|...`)
 * ============================================================================ */

function buildDemoFootprintAndGrid(
  lat: number,
  lon: number,
  scale = 25000,
  cols = 2,
  rows = 1
): { footprint: FeatureCollection; grid: FeatureCollection; sheetWKm: number; sheetHKm: number } {
  const sheetWKm = 4.65 * (scale / 25000);
  const sheetHKm = 6.55 * (scale / 25000);
  const degPerKmLat = 1 / 111.32;
  const degPerKmLon = 1 / (111.32 * Math.cos((lat * Math.PI) / 180));

  const totalW = sheetWKm * cols * degPerKmLon;
  const totalH = sheetHKm * rows * degPerKmLat;
  const startLon = lon - totalW / 2;
  const startLat = lat + totalH / 2;

  const rowNames = ["A", "B", "C"];
  const footprintFeatures: FeatureCollection["features"] = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const x0 = startLon + c * sheetWKm * degPerKmLon;
      const x1 = x0 + sheetWKm * degPerKmLon;
      const y1 = startLat - r * sheetHKm * degPerKmLat;
      const y0 = y1 - sheetHKm * degPerKmLat;
      const id = cols === 1 && rows === 1 ? "SHEET 1" : `${rowNames[r] || "A"}${c + 1}`;
      footprintFeatures.push({
        type: "Feature",
        properties: { sheet_id: id },
        geometry: {
          type: "Polygon",
          coordinates: [
            [
              [x0, y0],
              [x1, y0],
              [x1, y1],
              [x0, y1],
              [x0, y0],
            ],
          ],
        },
      });
    }
  }

  const gridFeatures: FeatureCollection["features"] = [];
  const totalKmW = Math.ceil(sheetWKm * cols);
  const totalKmH = Math.ceil(sheetHKm * rows);
  for (let i = 0; i <= totalKmW; i++) {
    const gx = startLon + i * degPerKmLon;
    gridFeatures.push({
      type: "Feature",
      properties: { label: String(41 + i).padStart(2, "0"), axis: "easting" },
      geometry: {
        type: "LineString",
        coordinates: [
          [gx, startLat - totalH],
          [gx, startLat],
        ],
      },
    });
  }
  for (let j = 0; j <= totalKmH; j++) {
    const gy = startLat - j * degPerKmLat;
    gridFeatures.push({
      type: "Feature",
      properties: { label: String(68 + (totalKmH - j)).padStart(2, "0"), axis: "northing" },
      geometry: {
        type: "LineString",
        coordinates: [
          [startLon, gy],
          [startLon + totalW, gy],
        ],
      },
    });
  }

  return {
    footprint: { type: "FeatureCollection", features: footprintFeatures },
    grid: { type: "FeatureCollection", features: gridFeatures },
    sheetWKm,
    sheetHKm,
  };
}

function buildDemoSheetPngBase64(page: number, title: string): string {
  if (typeof document === "undefined") return "";
  const c = document.createElement("canvas");
  c.width = 760;
  c.height = 1040;
  const ctx = c.getContext("2d");
  if (!ctx) return "";

  // Warm paper sheet background
  ctx.fillStyle = "#faf8f2";
  ctx.fillRect(0, 0, c.width, c.height);

  // Outer neatline
  ctx.strokeStyle = "#1e293b";
  ctx.lineWidth = 3;
  ctx.strokeRect(28, 28, 704, 984);

  // Header collar
  ctx.fillStyle = "#0f172a";
  ctx.font = "700 22px system-ui, sans-serif";
  ctx.fillText(title.toUpperCase(), 46, 66);
  ctx.font = "600 13px ui-monospace, monospace";
  ctx.fillStyle = "#475569";
  ctx.fillText(`PAGE ${page} · 1:25,000 TRUE-SCALE LOCAL UTM · EPSG:32756 · MGRS 56H LH`, 46, 88);
  ctx.fillStyle = "#b45309";
  ctx.textAlign = "right";
  ctx.fillText("WMM2025 G-M +12.4° E (+220 MILS)", 714, 66);
  ctx.textAlign = "left";

  // Map frame interior
  const mx = 46;
  const my = 106;
  const mw = 668;
  const mh = 760;
  ctx.fillStyle = "#eef4ea";
  ctx.fillRect(mx, my, mw, mh);

  // Synthetic topographic contour rings (pure cartographic demo preview)
  ctx.save();
  ctx.beginPath();
  ctx.rect(mx, my, mw, mh);
  ctx.clip();
  for (let i = 1; i <= 14; i++) {
    ctx.strokeStyle = i % 5 === 0 ? "#8d5524" : "#c08b5c";
    ctx.lineWidth = i % 5 === 0 ? 1.7 : 0.9;
    ctx.beginPath();
    ctx.ellipse(mx + 260, my + 310, i * 26, i * 19, -0.35, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.ellipse(mx + 520, my + 540, i * 22, i * 15, 0.45, 0, Math.PI * 2);
    ctx.stroke();
  }
  // Creek line
  ctx.strokeStyle = "#0284c7";
  ctx.lineWidth = 2.4;
  ctx.beginPath();
  ctx.moveTo(mx + 40, my + 690);
  ctx.bezierCurveTo(mx + 220, my + 560, mx + 370, my + 480, mx + 640, my + 120);
  ctx.stroke();

  // Trail line
  ctx.strokeStyle = "#b91c1c";
  ctx.lineWidth = 2.2;
  ctx.setLineDash([8, 5]);
  ctx.beginPath();
  ctx.moveTo(mx + 100, my + 180);
  ctx.quadraticCurveTo(mx + 310, my + 350, mx + 520, my + 540);
  ctx.stroke();
  ctx.setLineDash([]);

  // 1 km MGRS Grid lines
  ctx.strokeStyle = "rgba(3, 105, 161, 0.42)";
  ctx.lineWidth = 1;
  ctx.font = "700 11px ui-monospace, monospace";
  ctx.fillStyle = "#0369a1";
  for (let gx = 1; gx < 6; gx++) {
    const x = mx + gx * 112;
    ctx.beginPath();
    ctx.moveTo(x, my);
    ctx.lineTo(x, my + mh);
    ctx.stroke();
    ctx.fillText(String(42 + gx), x + 4, my + 16);
  }
  for (let gy = 1; gy < 7; gy++) {
    const y = my + gy * 110;
    ctx.beginPath();
    ctx.moveTo(mx, y);
    ctx.lineTo(mx + mw, y);
    ctx.stroke();
    ctx.fillText(String(74 - gy), mx + 6, y - 5);
  }
  ctx.restore();

  ctx.strokeStyle = "#0f172a";
  ctx.lineWidth = 2;
  ctx.strokeRect(mx, my, mw, mh);

  // Bottom collar scale bar & attribution
  ctx.fillStyle = "#0f172a";
  ctx.fillRect(46, 902, 160, 8);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(86, 903, 40, 6);
  ctx.fillRect(166, 903, 40, 6);
  ctx.fillStyle = "#0f172a";
  ctx.font = "700 11px ui-monospace, monospace";
  ctx.fillText("0        1        2        3        4 km", 46, 926);
  ctx.font = "500 11px system-ui, sans-serif";
  ctx.fillStyle = "#475569";
  ctx.fillText("Data: OpenStreetMap (ODbL) · ELVIS 5m DEM (CC BY 4.0) · NSW Spatial Trails · GridForge v1.7.1", 46, 962);
  ctx.fillText(page === 1 ? "SHEET A1 — PRIMARY TOPOGRAPHIC MAP" : `SUPPLEMENT PAGE ${page} — CALIBRATION & NAVIGATION CARD`, 46, 984);

  const dataUrl = c.toDataURL("image/png");
  return dataUrl.split(",")[1] || "";
}

function buildDemoPayload(view: KnownViewName): GridForgeViewPayload {
  switch (view) {
    case "studio":
      return {
        view: "studio",
        title: "GridForge Studio",
        summary: "True-scale UTM topographic maps, 10m MGRS, WMM2025 declination & RF Fresnel LOS",
        presets: DEFAULT_STUDIO_PRESETS,
        packs: [
          {
            pack_id: "nsw-blue-mountains",
            name: "NSW Blue Mountains Tactical",
            region: "AU · NSW",
            utm_zones: ["56S"],
            size_bytes: 342 * 1024 * 1024,
            has_basemap: true,
          },
          {
            pack_id: "nz-tongariro",
            name: "Tongariro Alpine Crossing",
            region: "NZ · North Island",
            utm_zones: ["60S"],
            size_bytes: 185 * 1024 * 1024,
            has_basemap: true,
          },
        ],
        public_url: "https://example.com",
      };

    case "briefing": {
      const lat = -33.7152;
      const lon = 150.3119;
      const { footprint, grid, sheetWKm, sheetHKm } = buildDemoFootprintAndGrid(lat, lon, 25000, 2, 1);
      return {
        view: "briefing",
        title: "Blue Mountains (Katoomba)",
        summary: "1:25,000 True-Scale Local UTM · 2×1 Atlas",
        question: "What are the MGRS coordinates, WMM2025 declination, and recommended open datasets for Katoomba?",
        answer:
          "Katoomba sits in UTM Zone 56S (EPSG:32756) at 10m MGRS 56H LH 4352 6945. Magnetic declination is +12.8° E with +0.4° grid convergence (G-M Angle +12.4° E / +220 mils). Recommended high-res stack: Geoscience Australia ELVIS 5m DEM + NSW State Trails + OSM Hydro.",
        location: {
          center: "-33.7152, 150.3119",
          lat,
          lon,
          mgrs_10m: "56H LH 4352 6945",
          utm_epsg: 32756,
          utm_zone: 56,
          utm_hemisphere: "S",
          gzd: "56H",
          square_ids_100km: ["LH"],
          place_name: "Blue Mountains (Katoomba)",
        },
        sheet: {
          scale: 25000,
          paper: "a4",
          orientation: "portrait",
          color_mode: "color",
          units: "metric",
          page_width_mm: 210,
          page_height_mm: 297,
          ground_width_km: sheetWKm,
          ground_height_km: sheetHKm,
          total_ground_width_km: sheetWKm * 2,
          total_ground_height_km: sheetHKm,
          grid_cols: 2,
          grid_rows: 1,
          sheet_count: 2,
          grid_spacing_mm: 40,
          dpi: 300,
        },
        declination: {
          model_name: "WMM2025",
          decimal_year: 2026.9,
          magnetic_declination_deg: 12.8,
          annual_change_deg: 0.02,
          grid_convergence_deg: 0.42,
          gm_angle_deg: 12.38,
          gm_angle_mils: 220,
        },
        recommendations: {
          dem_source: "au-elvis (5m)",
          region_label: "Australia · NSW",
          overlays: ["au-nsw-trails", "au-hydro", "au-parks"],
          matching_packs: ["nsw-blue-mountains"],
        },
        footprint_geojson: footprint,
        grid_geojson: grid,
        waypoints: [
          { label: "Echo Point", code: "W1", lat: -33.7322, lon: 150.3121, category: "waypoint" },
          { label: "Ruined Castle", code: "W2", lat: -33.7518, lon: 150.2945, category: "camp" },
        ],
        studio_url: "https://example.com/?center=-33.7152,150.3119&scale=25000",
      };
    }

    case "rf_link": {
      const distKm = 14.2;
      const n = 64;
      const samples = Array.from({ length: n }, (_, i) => {
        const t = i / (n - 1);
        const d = t * distKm;
        const los = 1045 + t * (1120 - 1045);
        // 1st Fresnel radius at 915 MHz: 17.32 * sqrt(d1*d2 / (f_ghz * D))
        const f1 = 17.32 * Math.sqrt((Math.max(0.001, d) * Math.max(0.001, distKm - d)) / (0.915 * distKm));
        // Synthetic mountain valley + central ridge grazing 60% Fresnel zone (~19m clearance)
        const valley = 940 - Math.sin(t * Math.PI) * 210 + Math.exp(-Math.pow((t - 0.56) / 0.11, 2)) * 334.5;
        const terrain = i === 0 ? 1042 : i === n - 1 ? 1117 : valley;
        return {
          dist_km: d,
          terrain_m: terrain,
          los_m: los,
          fresnel_hi_m: los + f1,
          fresnel_lo_m: los - f1,
          fresnel60_lo_m: los - f1 * 0.6,
          clearance_m: los - terrain,
        };
      });
      return {
        view: "rf_link",
        title: "915 MHz LoRa · Narrow Neck → Mt Solitary",
        summary: "Curved-Earth (k=4/3) 1st Fresnel Zone Terrain Analysis",
        answer:
          "MARGINAL link across Jamison Valley: optical Line-of-Sight clears the central ridge by +19.4m, preserving 54% of the 1st Fresnel zone (just under the 60% clear threshold). Raising either mast by +3m achieves full 60% F1 clearance.",
        request: {
          from_lat: -33.7485,
          from_lon: 150.2891,
          to_lat: -33.8112,
          to_lon: 150.3914,
          freq_mhz: 915,
          from_height_m: 3,
          to_height_m: 3,
          from_label: "NARROW NECK",
          to_label: "MT SOLITARY",
          dem_source: "au-elvis",
        },
        profile: {
          status: "MARGINAL",
          distance_km: distKm,
          freq_mhz: 915,
          fspl_db: 114.7,
          min_clearance_m: 19.4,
          min_fresnel_pct: 54,
          los_clear: true,
          fresnel_60pct_clear: false,
          bearing_true_deg: 126.4,
          bearing_grid_deg: 126.0,
          bearing_mag_deg: 113.6,
          bearing_mils: 2020,
          from_elev_m: 1042,
          to_elev_m: 1117,
          from_mgrs: "56H LH 4148 6572",
          to_mgrs: "56H LH 5106 5891",
          from_label: "NARROW NECK",
          to_label: "MT SOLITARY",
          from_height_m: 3,
          to_height_m: 3,
          samples,
        },
        studio_url: "https://example.com/?center=-33.780,150.340&scale=50000",
      };
    }

    case "viewshed": {
      const lat = -36.4559;
      const lon = 148.2636;
      const rDegLat = 5 / 111.32;
      const rDegLon = 5 / (111.32 * Math.cos((lat * Math.PI) / 180));
      const ringCoords = Array.from({ length: 65 }, (_, i) => {
        const a = (i / 64) * Math.PI * 2;
        return [lon + Math.cos(a) * rDegLon, lat + Math.sin(a) * rDegLat];
      });
      const vsCoords = Array.from({ length: 49 }, (_, i) => {
        const a = (i / 48) * Math.PI * 2;
        const radScale = 0.42 + 0.48 * Math.abs(Math.sin(a * 2.5 + 0.4)) * (0.65 + 0.35 * Math.cos(a * 3));
        return [lon + Math.cos(a) * rDegLon * radScale, lat + Math.sin(a) * rDegLat * radScale];
      });
      vsCoords[vsCoords.length - 1] = vsCoords[0]!;
      return {
        view: "viewshed",
        title: "Viewshed · Mt Kosciuszko Summit",
        summary: "Terrain-masked 5.0 km optical / RF coverage (EPSG:32755)",
        answer:
          "From Mt Kosciuszko Summit (2228m, +3m mast), 48.6 km² (62% of the 5 km radius disc) has direct terrain-masked line-of-sight.",
        observer: {
          label: "KOSCIUSZKO",
          lat,
          lon,
          observer_height_m: 3,
          target_height_m: 2,
          max_distance_m: 5000,
        },
        result: {
          observer_mgrs_10m: "55H FV 1326 6543",
          observer_elev_m: 2228,
          utm_epsg: 32755,
          max_distance_m: 5000,
          visible_area_km2: 48.62,
          viewshed_geojson: {
            type: "FeatureCollection",
            features: [
              { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [vsCoords] } },
            ],
          },
          range_ring_geojson: {
            type: "FeatureCollection",
            features: [
              { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: ringCoords } },
            ],
          },
        },
        studio_url: "https://example.com/?center=-36.4559,148.2636&scale=25000",
      };
    }

    case "route": {
      const wps = [
        { seq: 1, code: "W1", label: "Mangatepopo", category: "waypoint" as const, lat: -39.1462, lon: 175.5812, mgrs_10m: "60H UG 7742 6490" },
        { seq: 2, code: "W2", label: "Red Crater", category: "hazard" as const, lat: -39.1342, lon: 175.6498, mgrs_10m: "60H UG 8331 6640" },
        { seq: 3, code: "W3", label: "Emerald Lakes", category: "water" as const, lat: -39.1294, lon: 175.6545, mgrs_10m: "60H UG 8370 6695" },
        { seq: 4, code: "W4", label: "Ketetahi Hut", category: "camp" as const, lat: -39.1066, lon: 175.6416, mgrs_10m: "60H UG 8252 6945" },
      ];
      const { footprint, grid } = buildDemoFootprintAndGrid(-39.128, 175.622, 25000, 2, 1);
      footprint.features.forEach((f, idx) => {
        if (f.properties) f.properties.sheet_id = `S${idx + 1}`;
      });
      return {
        view: "route",
        title: "Tongariro Alpine Crossing · Strip Atlas",
        summary: "11.45 km corridor across 2 true-scale UTM strip sheets",
        answer: "Computed 3 route legs (11.45 km total) covered by 2 sequential 1:25,000 A4 strip sheets in UTM Zone 60S.",
        waypoints: wps,
        route_metrics: {
          waypoints: wps,
          total_distance_km: 11.45,
          total_distance_mi: 7.11,
          legs: [
            { from_code: "W1", to_code: "W2", leg_dist_km: 6.08, cum_dist_km: 6.08, true_az_deg: 77.2, grid_az_deg: 76.5, mag_az_deg: 55.1, mag_az_mils: 980 },
            { from_code: "W2", to_code: "W3", leg_dist_km: 0.67, cum_dist_km: 6.75, true_az_deg: 37.5, grid_az_deg: 36.8, mag_az_deg: 15.4, mag_az_mils: 274 },
            { from_code: "W3", to_code: "W4", leg_dist_km: 4.70, cum_dist_km: 11.45, true_az_deg: 336.4, grid_az_deg: 335.7, mag_az_deg: 314.3, mag_az_mils: 5588 },
          ],
        },
        strip_plan: {
          utm_epsg: 32760,
          utm_zone: 60,
          utm_hemisphere: "S",
          gzd: "60H",
          route_distance_km: 11.45,
          sheet_count: 2,
          sheets: [
            { sheet_id: "S1", index: 1, center_lat: -39.140, center_lon: 175.612, center_mgrs_10m: "60H UG 8008 6566" },
            { sheet_id: "S2", index: 2, center_lat: -39.118, center_lon: 175.648, center_mgrs_10m: "60H UG 8311 6819" },
          ],
        },
        footprint_geojson: footprint,
        grid_geojson: grid,
        studio_url: "https://example.com/?center=-39.1328,175.6422&scale=25000",
      };
    }

    case "render":
      return {
        view: "render",
        title: "Blue Mountains (Katoomba) · 1:25,000 Print Sheet",
        summary: "300 DPI Vector GeoPDF + ATAK MBTiles + Garmin KMZ",
        answer:
          "Completed true-scale 1:25,000 A4 Portrait Color print render in 4.8s (0.04 mm scale error). Ready to inspect and download.",
        page_count: 2,
        pages: [
          { page: 1, preview_url: "/api/jobs/demo-job-1/pages/1.png" },
          { page: 2, preview_url: "/api/jobs/demo-job-1/pages/2.png" },
        ],
        job: {
          job_id: "demo-job-1",
          status: "succeeded",
          stage: "completed",
          progress_pct: 100,
          title: "Blue Mountains (Katoomba)",
          center: "-33.7152, 150.3119",
          center_mgrs: "56H LH 4352 6945",
          scale: 25000,
          paper: "a4",
          orientation: "portrait",
          color_mode: "color",
          dpi: 300,
          grid_cols: 1,
          grid_rows: 1,
          elapsed_seconds: 4.8,
          artifacts: [
            { kind: "pdf", filename: "katoomba_1-25000_a4_color.pdf", size_bytes: 4_380_000, download_url: "https://example.com/api/jobs/demo-job-1/artifacts/katoomba.pdf?sig=demo" },
            { kind: "mbtiles", filename: "katoomba_atak.mbtiles", size_bytes: 11_200_000, download_url: "https://example.com/api/jobs/demo-job-1/artifacts/katoomba.mbtiles?sig=demo" },
            { kind: "kmz", filename: "katoomba_garmin.kmz", size_bytes: 3_140_000, download_url: "https://example.com/api/jobs/demo-job-1/artifacts/katoomba.kmz?sig=demo" },
            { kind: "zip", filename: "katoomba_field_bundle.zip", size_bytes: 18_700_000, download_url: "https://example.com/api/jobs/demo-job-1/artifacts/katoomba_bundle.zip?sig=demo" },
          ],
        },
        studio_url: "https://example.com/?center=-33.7152,150.3119&scale=25000",
      };

    case "geocode":
      return {
        view: "geocode",
        title: "Place Search Results",
        query: "Kosciuszko",
        results: [
          { short_name: "Mount Kosciuszko", display_name: "Mount Kosciuszko, Kosciuszko National Park, NSW, Australia", lat: -36.4559, lon: 148.2636, type: "peak" },
          { short_name: "Kosciuszko National Park", display_name: "Kosciuszko National Park, Snowy Monaro, NSW, Australia", lat: -36.2089, lon: 148.4622, type: "national_park" },
          { short_name: "Charlotte Pass", display_name: "Charlotte Pass Village, Snowy Mountains, NSW, Australia", lat: -36.4337, lon: 148.3292, type: "village" },
        ],
      };

    case "packs":
      return {
        view: "packs",
        title: "Offline Regional Packs",
        summary: "2 installed local packs · 29 curated AU / NZ / NA regions",
        packs: [
          {
            pack_id: "nsw-blue-mountains",
            name: "NSW Blue Mountains Tactical",
            region: "au-nsw-blue-mountains",
            utm_zones: ["56S"],
            size_bytes: 342 * 1024 * 1024,
            has_basemap: true,
            bbox_wgs84: { min_x: 150.1, min_y: -33.9, max_x: 150.5, max_y: -33.5 },
          },
        ],
        regions: [
          { region_id: "au-nsw-blue-mountains", name: "Blue Mountains & Wollemi", group: "au_tactical", utm_zones: ["56S"], estimated_size_hint: "~340 MB", bbox_wgs84: { min_x: 150.1, min_y: -33.9, max_x: 150.5, max_y: -33.5 } },
          { region_id: "au-nsw-snowy", name: "Snowy Mountains & Kosciuszko", group: "au_tactical", utm_zones: ["55S"], estimated_size_hint: "~290 MB", bbox_wgs84: { min_x: 148.1, min_y: -36.7, max_x: 148.6, max_y: -36.1 } },
          { region_id: "nz-tongariro", name: "Tongariro Alpine Crossing", group: "nz", utm_zones: ["60S"], estimated_size_hint: "~185 MB", bbox_wgs84: { min_x: 175.4, min_y: -39.3, max_x: 175.8, max_y: -39.0 } },
          { region_id: "na-us-yosemite", name: "Yosemite & High Sierra", group: "na", utm_zones: ["11N"], estimated_size_hint: "~420 MB", bbox_wgs84: { min_x: -119.8, min_y: 37.6, max_x: -119.3, max_y: 37.9 } },
          { region_id: "na-ca-banff", name: "Banff & Lake Louise", group: "na", utm_zones: ["11N"], estimated_size_hint: "~380 MB", bbox_wgs84: { min_x: -116.4, min_y: 51.1, max_x: -115.4, max_y: 51.6 } },
        ],
      };
  }
}

async function runDemoTool(name: string, args: Record<string, unknown>): Promise<unknown> {
  if (name === "app_get_job_page") {
    const pg = Number(args.page || 1);
    const title =
      state.payload?.view === "render"
        ? state.payload.job.title || "GridForge Print Sheet"
        : "GridForge Print Sheet";
    return {
      structuredContent: {
        job_id: String(args.job_id || "demo-job-1"),
        page: pg,
        page_count: 2,
        mime_type: "image/png",
        data_base64: buildDemoSheetPngBase64(pg, title),
      },
    };
  }
  if (name === "app_get_job_status") {
    const r = buildDemoPayload("render") as RenderViewPayload;
    return { structuredContent: { job: r.job, page_count: r.page_count, pages: r.pages } };
  }
  if (name === "geocode_place") {
    const g = buildDemoPayload("geocode") as GeocodeViewPayload;
    return { structuredContent: { ...g, query: String(args.query || g.query) } };
  }
  if (name === "list_packs") {
    return { structuredContent: buildDemoPayload("packs") };
  }
  if (name === "render_map") {
    const r = buildDemoPayload("render") as RenderViewPayload;
    r.job = {
      ...r.job,
      scale: Number(args.scale || r.job.scale),
      paper: (args.paper as PaperSize) || r.job.paper,
      orientation: (args.orientation as Orientation) || r.job.orientation,
      color_mode: (args.color_mode as ColorMode) || r.job.color_mode,
      title: (args.title as string) || r.job.title,
    };
    return { structuredContent: r };
  }
  if (name === "analyze_rf_link") {
    const rf = buildDemoPayload("rf_link") as RFLinkViewPayload;
    const freq = Number(args.freq_mhz || rf.request.freq_mhz);
    const txH = Number(args.from_height_m ?? rf.request.from_height_m);
    const rxH = Number(args.to_height_m ?? rf.request.to_height_m);
    const bonus = (txH + rxH - 6) * 0.65;
    const nextClear = rf.profile.min_clearance_m + bonus;
    const nextStatus = nextClear >= 23 ? "CLEAR" : nextClear >= 0 ? "MARGINAL" : "OBSTRUCTED";
    rf.request = { ...rf.request, freq_mhz: freq, from_height_m: txH, to_height_m: rxH };
    rf.profile = {
      ...rf.profile,
      freq_mhz: freq,
      from_height_m: txH,
      to_height_m: rxH,
      min_clearance_m: nextClear,
      min_fresnel_pct: Math.max(0, Math.round(54 + bonus * 2.4)),
      status: nextStatus,
      fresnel_60pct_clear: nextStatus === "CLEAR",
    };
    return { structuredContent: rf };
  }
  if (name === "compute_viewshed") {
    const vs = buildDemoPayload("viewshed") as ViewshedViewPayload;
    const radM = Number(args.max_distance_m || vs.observer.max_distance_m);
    vs.observer = {
      ...vs.observer,
      observer_height_m: Number(args.observer_height_m ?? vs.observer.observer_height_m),
      target_height_m: Number(args.target_height_m ?? vs.observer.target_height_m),
      max_distance_m: radM,
    };
    vs.result = {
      ...vs.result,
      max_distance_m: radM,
      visible_area_km2: Number((48.62 * Math.pow(radM / 5000, 1.7)).toFixed(2)),
    };
    return { structuredContent: vs };
  }
  // preview_footprint or answer_location_briefing
  const b = buildDemoPayload("briefing") as BriefingViewPayload;
  const centerStr = String(args.center || args.location || b.location.center);
  const parts = centerStr.split(",").map((s) => Number(s.trim()));
  const lat = parts.length === 2 && Number.isFinite(parts[0]) ? parts[0]! : b.location.lat;
  const lon = parts.length === 2 && Number.isFinite(parts[1]) ? parts[1]! : b.location.lon;
  const scale = Number(args.scale || b.sheet.scale);
  const cols = Number(args.grid_cols || b.sheet.grid_cols || 1);
  const rows = Number(args.grid_rows || b.sheet.grid_rows || 1);
  const { footprint, grid, sheetWKm, sheetHKm } = buildDemoFootprintAndGrid(lat, lon, scale, cols, rows);
  b.location = { ...b.location, lat, lon, center: `${lat.toFixed(5)}, ${lon.toFixed(5)}` };
  b.sheet = {
    ...b.sheet,
    scale,
    paper: (args.paper as PaperSize) || b.sheet.paper,
    orientation: (args.orientation as Orientation) || b.sheet.orientation,
    color_mode: (args.color_mode as ColorMode) || b.sheet.color_mode,
    grid_cols: cols,
    grid_rows: rows,
    sheet_count: cols * rows,
    ground_width_km: sheetWKm,
    ground_height_km: sheetHKm,
    total_ground_width_km: sheetWKm * cols,
    total_ground_height_km: sheetHKm * rows,
  };
  b.footprint_geojson = footprint;
  b.grid_geojson = grid;
  return { structuredContent: b };
}

function parseDemoHash(): { view: KnownViewName | null; theme: "dark" | "light" | null } {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return { view: null, theme: null };
  const params = new URLSearchParams(hash);
  const rawView = params.get("demo");
  const rawTheme = params.get("theme");
  const validViews: KnownViewName[] = [
    "studio",
    "briefing",
    "rf_link",
    "viewshed",
    "route",
    "render",
    "geocode",
    "packs",
  ];
  const view = rawView && validViews.includes(rawView as KnownViewName)
    ? (rawView as KnownViewName)
    : null;
  const theme = rawTheme === "light" || rawTheme === "dark" ? rawTheme : null;
  return { view, theme };
}

function activateDemoMode(view: KnownViewName, theme: "dark" | "light" | null): void {
  state.demoMode = true;
  syncCapabilities();
  if (theme) {
    applyDocumentTheme(theme);
  }
  setPayload(buildDemoPayload(view));
}

/* ============================================================================
 * Bootstrap — Register ALL MCP App handlers BEFORE app.connect()
 * ============================================================================ */

async function bootstrap(): Promise<void> {
  // 1. Standalone preview hash (#demo=briefing&theme=light) activates immediately
  const demo = parseDemoHash();
  if (demo.view) {
    activateDemoMode(demo.view, demo.theme);
    window.addEventListener("hashchange", () => {
      const next = parseDemoHash();
      if (next.view) activateDemoMode(next.view, next.theme);
    });
    return;
  }

  // 2. Register ALL MCP App handlers BEFORE calling await app.connect()!
  app.ontoolinput = (params) => {
    const argPreview = params.arguments
      ? Object.entries(params.arguments)
          .slice(0, 4)
          .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
          .join(" · ")
      : "";
    state.busy = true;
    state.busyLabel = "Running GridForge tool…";
    if (!state.payload) {
      const root = getRootEl();
      if (root) {
        root.innerHTML = renderLoadingShell("Running GridForge…", argPreview);
      }
    } else {
      renderCurrentView();
    }
  };

  app.ontoolresult = (result) => {
    state.busy = false;
    state.busyLabel = "";
    handleToolResultPayload(result);
  };

  app.ontoolcancelled = (params) => {
    stopPolling();
    state.busy = false;
    state.busyLabel = "";
    const reason = params?.reason ? `: ${params.reason}` : "";
    if (!state.payload) {
      const root = getRootEl();
      if (root) {
        root.innerHTML = `
          ${renderHeader("GridForge", "Tool execution cancelled")}
          <section class="gf-card">
            <div class="gf-empty-panel">Tool call cancelled${escapeHtml(reason)}.</div>
          </section>
        `;
      }
    } else {
      showToast(`Tool call cancelled${reason}`, true);
    }
  };

  app.onhostcontextchanged = (ctx) => {
    applyHostContext(ctx);
  };

  app.onteardown = async () => {
    stopPolling();
    cleanupInteractiveMounts();
    return {};
  };

  // Initial connecting state
  renderCurrentView();

  // Standalone browser fallback ONLY when opened directly outside an iframe
  const isStandaloneTopWindow = window.parent === window;
  let fallbackTimer: number | null = null;
  if (isStandaloneTopWindow) {
    fallbackTimer = window.setTimeout(() => {
      if (!state.connected && !state.payload) {
        const prefersLight =
          window.matchMedia &&
          window.matchMedia("(prefers-color-scheme: light)").matches;
        activateDemoMode("studio", prefersLight ? "light" : "dark");
      }
    }, 650);
  }

  try {
    await app.connect();
    if (fallbackTimer !== null) window.clearTimeout(fallbackTimer);
    state.connected = true;
    syncCapabilities();
    applyHostContext(app.getHostContext());
  } catch (err) {
    if (fallbackTimer !== null) window.clearTimeout(fallbackTimer);
    if (isStandaloneTopWindow && !state.payload) {
      activateDemoMode("studio", null);
      return;
    }
    const root = getRootEl();
    if (root) {
      root.innerHTML = `
        ${renderHeader("GridForge", "Connection Error")}
        <section class="gf-card">
          <div class="gf-empty-panel">Could not connect to MCP host: ${escapeHtml(err instanceof Error ? err.message : String(err))}</div>
        </section>
      `;
    }
  }
}

if (typeof document !== "undefined") {
  void bootstrap();
}
