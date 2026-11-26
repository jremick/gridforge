import { formatBytes } from "./presets.ts";
import type {
  AppFormState,
  OutputKey,
  SessionCapabilities,
  SessionResponse,
} from "./types.ts";

export const DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

const OUTPUT_KEYS: readonly OutputKey[] = [
  "pdf",
  "geopdf",
  "mbtiles",
  "kmz",
  "gpkg_tiles",
  "osmand_sqlitedb",
  "field_bundle_zip",
];

const PRIMARY_OUTPUT_KEYS: readonly OutputKey[] = [
  "pdf",
  "mbtiles",
  "kmz",
  "gpkg_tiles",
  "osmand_sqlitedb",
  "field_bundle_zip",
];

export function createDefaultNoneSession(): SessionResponse {
  return {
    auth_mode: "none",
    authenticated: false,
    user: null,
    role: "none",
    capabilities: {
      max_grid_cells: 9,
      max_grid_cols: 3,
      max_grid_rows: 3,
      strip_atlas_allowed: true,
      max_strip_sheets: 24,
      allowed_outputs: [...OUTPUT_KEYS],
      packs_manage_allowed: true,
      max_upload_bytes: DEFAULT_MAX_UPLOAD_BYTES,
      max_dpi: 600,
      max_export_dpi: 600,
      turnstile_required: false,
      turnstile_sitekey: null,
      google_oauth_enabled: false,
    },
    quotas: {
      daily_renders_limit: null,
      daily_renders_used: 0,
      daily_renders_remaining: null,
      hourly_previews_limit: null,
      hourly_previews_used: 0,
      hourly_previews_remaining: null,
      public_renders_paused: false,
      global_daily_public_limit: null,
      global_daily_public_used: 0,
    },
  };
}

export function isAtlasOptionAllowed(
  cols: number,
  rows: number,
  cap: SessionCapabilities
): boolean {
  if (cols < 1 || rows < 1) return false;
  if (cols > cap.max_grid_cols || rows > cap.max_grid_rows) return false;
  return cols * rows <= cap.max_grid_cells;
}

export function isOutputAllowed(
  key: OutputKey,
  cap: SessionCapabilities
): boolean {
  return cap.allowed_outputs.includes(key);
}

export function getEffectiveMaxDpi(cap: SessionCapabilities): number {
  return typeof cap.max_dpi === "number" && Number.isFinite(cap.max_dpi)
    ? cap.max_dpi
    : 600;
}

export function getEffectiveMaxExportDpi(cap: SessionCapabilities): number {
  if (
    typeof cap.max_export_dpi === "number" &&
    Number.isFinite(cap.max_export_dpi)
  ) {
    return cap.max_export_dpi;
  }
  return getEffectiveMaxDpi(cap);
}

export function clampFormStateToCapabilities(
  state: AppFormState,
  cap: SessionCapabilities
): { changed: boolean; resetItems: string[] } {
  const resetItems: string[] = [];
  let changed = false;

  if (!isAtlasOptionAllowed(state.grid_cols, state.grid_rows, cap)) {
    state.grid_cols = 1;
    state.grid_rows = 1;
    if (!state.stripPlan) {
      state.include_atlas_index = false;
    }
    resetItems.push("atlas grid");
    changed = true;
  }

  if (!cap.strip_atlas_allowed && state.stripPlan !== null) {
    state.stripPlan = null;
    if (state.grid_cols * state.grid_rows <= 1) {
      state.include_atlas_index = false;
    }
    resetItems.push("strip atlas");
    changed = true;
  }

  for (const key of OUTPUT_KEYS) {
    if (state.outputs[key] && !isOutputAllowed(key, cap)) {
      state.outputs[key] = false;
      resetItems.push(key);
      changed = true;
    }
  }

  const hasPrimary = PRIMARY_OUTPUT_KEYS.some((key) => state.outputs[key]);
  if (!hasPrimary && isOutputAllowed("pdf", cap)) {
    state.outputs.pdf = true;
    changed = true;
  }

  if (!state.outputs.pdf && state.outputs.geopdf) {
    state.outputs.geopdf = false;
    changed = true;
  }

  if (state.overlayFile && state.overlayFile.size > cap.max_upload_bytes) {
    state.overlayFile = null;
    resetItems.push("overlay file");
    changed = true;
  }

  const maxDpi = getEffectiveMaxDpi(cap);
  if (state.dpi > maxDpi) {
    state.dpi = maxDpi;
    resetItems.push("print DPI");
    changed = true;
  }

  const maxExportDpi = getEffectiveMaxExportDpi(cap);
  if (state.outputs.export_dpi > maxExportDpi) {
    state.outputs.export_dpi = maxExportDpi;
    resetItems.push("export DPI");
    changed = true;
  }

  return { changed, resetItems };
}

export function formatUploadLimitLabel(maxUploadBytes: number): string {
  if (!Number.isFinite(maxUploadBytes) || maxUploadBytes <= 0) {
    return "0 B";
  }
  const mib = 1024 * 1024;
  if (maxUploadBytes >= mib && maxUploadBytes % mib === 0) {
    return `${maxUploadBytes / mib} MB`;
  }
  return formatBytes(maxUploadBytes);
}

export interface AccountPillView {
  roleLabel: string;
  displayHandle: string;
  initials: string;
  quotaShort: string;
  quotaFull: string;
  title: string;
  ariaLabel: string;
  exhausted: boolean;
  paused: boolean;
  publicPaused: boolean;
}

function deriveUserIdentity(session: SessionResponse): {
  displayHandle: string;
  initials: string;
} {
  const rawName = session.user?.display_name?.trim() ?? "";
  const rawEmail = session.user?.email?.trim() ?? "";
  const emailLocal = (
    rawEmail.includes("@") ? rawEmail.split("@", 1)[0] ?? "" : rawEmail
  ).trim();
  const genericRoleNames = new Set(["admin", "friend", "guest", "local"]);
  const preferred =
    rawName && !genericRoleNames.has(rawName.toLowerCase())
      ? rawName
      : emailLocal || rawName || session.role;

  const cleanParts = preferred
    .split(/[\s._-]+/)
    .map((p) => p.replace(/[^A-Za-z0-9]/g, ""))
    .filter(Boolean);

  let initials = "U";
  const firstPart = cleanParts[0];
  const secondPart = cleanParts[1];
  if (firstPart && secondPart) {
    initials = `${firstPart.charAt(0)}${secondPart.charAt(0)}`.toUpperCase();
  } else if (firstPart) {
    initials = firstPart.slice(0, 2).toUpperCase();
  }

  return {
    displayHandle: preferred || "Account",
    initials,
  };
}

export function isGuestRenderPaused(session: SessionResponse): boolean {
  return (
    session.auth_mode === "hosted" &&
    Boolean(session.quotas.public_renders_paused) &&
    session.role === "guest"
  );
}

export function formatAccountPill(session: SessionResponse): AccountPillView {
  const q = session.quotas;
  const publicPaused = Boolean(q.public_renders_paused);
  const paused = publicPaused && session.role === "guest";

  if (session.role === "none") {
    return {
      roleLabel: "Local",
      displayHandle: "Local",
      initials: "LO",
      quotaShort: "",
      quotaFull: "",
      title: "Zero-login local mode",
      ariaLabel: "Local mode",
      exhausted: false,
      paused: false,
      publicPaused: false,
    };
  }

  if (session.role === "admin") {
    const { displayHandle, initials } = deriveUserIdentity(session);
    const actor =
      session.user?.display_name?.trim() || session.user?.email || "Admin";
    const pauseNote = publicPaused ? " · Public renders PAUSED" : "";
    return {
      roleLabel: "Admin",
      displayHandle,
      initials,
      quotaShort: "",
      quotaFull: "",
      title: `${actor} · Admin · Unlimited renders & packs${pauseNote}`,
      ariaLabel: `Account: Admin${publicPaused ? ", public renders paused" : ""}`,
      exhausted: false,
      paused: false,
      publicPaused,
    };
  }

  const dailyLim = q.daily_renders_limit;
  const dailyRem =
    dailyLim === null
      ? null
      : Math.max(
          0,
          q.daily_renders_remaining ?? dailyLim - q.daily_renders_used
        );
  const prevLim = q.hourly_previews_limit;
  const prevRem =
    prevLim === null
      ? null
      : Math.max(
          0,
          q.hourly_previews_remaining ?? prevLim - q.hourly_previews_used
        );

  const quotaShort =
    dailyLim !== null && dailyRem !== null ? `${dailyRem}/${dailyLim}` : "";
  const quotaFull = quotaShort ? `${quotaShort} left` : "";
  const exhausted = dailyRem !== null && dailyRem <= 0;

  if (session.role === "friend") {
    const { displayHandle, initials } = deriveUserIdentity(session);
    const actor =
      session.user?.display_name?.trim() || session.user?.email || "Friend";
    const renderPart =
      dailyLim !== null && dailyRem !== null
        ? `${dailyRem}/${dailyLim} daily renders`
        : "Unlimited daily renders";
    const previewPart =
      prevLim !== null && prevRem !== null
        ? `${prevRem}/${prevLim} hourly previews`
        : "Unlimited previews";
    const pausePart = paused ? " · Public renders paused" : "";
    return {
      roleLabel: "Friend",
      displayHandle,
      initials,
      quotaShort,
      quotaFull,
      title: `${actor} · Friend · ${renderPart} · ${previewPart} · 14d retention${pausePart}`,
      ariaLabel: `Account: Friend${quotaFull ? `, ${quotaFull}` : ""}${paused ? ", paused" : ""}`,
      exhausted,
      paused,
      publicPaused,
    };
  }

  // Guest
  const renderPart =
    dailyLim !== null && dailyRem !== null
      ? `${dailyRem}/${dailyLim} daily renders remaining`
      : "Public guest access";
  const previewPart =
    prevLim !== null && prevRem !== null
      ? ` · ${prevRem}/${prevLim} hourly previews`
      : "";
  const pausePart = paused ? " · Public renders paused" : "";
  return {
    roleLabel: "Guest",
    displayHandle: "Guest",
    initials: "G",
    quotaShort,
    quotaFull,
    title: `Public Guest · ${renderPart}${previewPart} · 24h retention${pausePart}`,
    ariaLabel: `Account: Guest${quotaFull ? `, ${quotaFull}` : ""}${paused ? ", paused" : ""}`,
    exhausted,
    paused,
    publicPaused,
  };
}

export function extractInviteTokenFromSearch(search: string): string | null {
  const params = new URLSearchParams(search);
  const raw = params.get("invite") ?? params.get("invite_token");
  const trimmed = raw?.trim() ?? "";
  return trimmed ? trimmed : null;
}

export function extractAuthErrorFromSearch(search: string): string | null {
  const params = new URLSearchParams(search);
  const raw = params.get("auth_error") ?? params.get("error");
  const trimmed = raw?.trim() ?? "";
  return trimmed ? trimmed : null;
}

export function buildGoogleOAuthUrl(inviteToken?: string | null): string {
  const trimmed = inviteToken?.trim() ?? "";
  if (trimmed) {
    return `/api/auth/google/start?invite_token=${encodeURIComponent(trimmed)}`;
  }
  return "/api/auth/google/start";
}

export type ApiErrorKind =
  | "auth"
  | "turnstile"
  | "forbidden"
  | "upload_too_large"
  | "rate_limit"
  | "service_unavailable"
  | "unknown";

export interface ApiErrorClassification {
  status: number;
  message: string;
  kind: ApiErrorKind;
  shouldPromptSignIn: boolean;
  shouldRefreshSession: boolean;
  shouldResetTurnstile: boolean;
}

export function classifyApiError(err: unknown): ApiErrorClassification {
  let status = 0;
  let message = "Request failed";

  if (err instanceof Error) {
    message = err.message || message;
    if (
      "status" in err &&
      typeof (err as { status?: unknown }).status === "number"
    ) {
      status = (err as { status: number }).status;
    }
  } else if (err && typeof err === "object") {
    const rec = err as { status?: unknown; message?: unknown };
    if (typeof rec.status === "number") status = rec.status;
    if (typeof rec.message === "string" && rec.message) message = rec.message;
  }

  if (status === 401) {
    return {
      status,
      message,
      kind: "auth",
      shouldPromptSignIn: true,
      shouldRefreshSession: true,
      shouldResetTurnstile: false,
    };
  }

  if (status === 403) {
    const isTurnstile =
      /turnstile|verification|challenge|captcha/i.test(message);
    return {
      status,
      message,
      kind: isTurnstile ? "turnstile" : "forbidden",
      shouldPromptSignIn: false,
      shouldRefreshSession: !isTurnstile,
      shouldResetTurnstile: isTurnstile,
    };
  }

  if (status === 413) {
    return {
      status,
      message,
      kind: "upload_too_large",
      shouldPromptSignIn: false,
      shouldRefreshSession: false,
      shouldResetTurnstile: false,
    };
  }

  if (status === 429) {
    return {
      status,
      message,
      kind: "rate_limit",
      shouldPromptSignIn: false,
      shouldRefreshSession: true,
      shouldResetTurnstile: false,
    };
  }

  if (status === 503) {
    return {
      status,
      message,
      kind: "service_unavailable",
      shouldPromptSignIn: false,
      shouldRefreshSession: true,
      shouldResetTurnstile: false,
    };
  }

  return {
    status,
    message,
    kind: "unknown",
    shouldPromptSignIn: false,
    shouldRefreshSession: false,
    shouldResetTurnstile: false,
  };
}
