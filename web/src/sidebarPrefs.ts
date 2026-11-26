export const DEFAULT_SIDEBAR_WIDTH = 368;
export const MIN_SIDEBAR_WIDTH = 300;
export const MAX_SIDEBAR_WIDTH = 560;
export const SIDEBAR_MOBILE_BREAKPOINT = 680;
export const SIDEBAR_MAX_VIEWPORT_RATIO = 0.55;

const STORAGE_PREFIX = "gridforge.ui.sidebar.v1";

export interface SidebarPrefs {
  width: number;
  collapsed: boolean;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function getMaxSidebarWidthForViewport(viewportWidth: number): number {
  if (!Number.isFinite(viewportWidth) || viewportWidth <= 0) {
    return MAX_SIDEBAR_WIDTH;
  }
  if (viewportWidth <= SIDEBAR_MOBILE_BREAKPOINT) {
    return Math.max(240, Math.min(360, Math.floor(viewportWidth)));
  }
  const viewportCap = Math.floor(viewportWidth * SIDEBAR_MAX_VIEWPORT_RATIO);
  return Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, viewportCap));
}

export function clampSidebarWidth(
  width: number,
  viewportWidth = 1440
): number {
  const maxWidth = getMaxSidebarWidthForViewport(viewportWidth);
  const minWidth =
    viewportWidth <= SIDEBAR_MOBILE_BREAKPOINT
      ? Math.min(MIN_SIDEBAR_WIDTH, maxWidth)
      : MIN_SIDEBAR_WIDTH;
  if (!Number.isFinite(width)) {
    return Math.max(minWidth, Math.min(maxWidth, DEFAULT_SIDEBAR_WIDTH));
  }
  return Math.round(Math.max(minWidth, Math.min(maxWidth, width)));
}

export function sidebarStorageKey(userId?: string | null): string {
  const trimmed = userId?.trim() ?? "";
  return trimmed ? `${STORAGE_PREFIX}:user:${trimmed}` : `${STORAGE_PREFIX}:browser`;
}

function getDefaultStorage(): StorageLike | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      return window.localStorage;
    }
  } catch {
    return null;
  }
  return null;
}

function parsePrefsJson(
  raw: string | null,
  viewportWidth: number
): SidebarPrefs | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<SidebarPrefs> | null;
    if (!parsed || typeof parsed !== "object") return null;
    const width =
      typeof parsed.width === "number"
        ? clampSidebarWidth(parsed.width, viewportWidth)
        : clampSidebarWidth(DEFAULT_SIDEBAR_WIDTH, viewportWidth);
    const collapsed =
      typeof parsed.collapsed === "boolean" ? parsed.collapsed : false;
    return { width, collapsed };
  } catch {
    return null;
  }
}

export function loadSidebarPrefs(
  userId?: string | null,
  viewportWidth = 1440,
  storage: StorageLike | null = getDefaultStorage()
): SidebarPrefs {
  const fallback: SidebarPrefs = {
    width: clampSidebarWidth(DEFAULT_SIDEBAR_WIDTH, viewportWidth),
    collapsed: false,
  };
  if (!storage) return fallback;

  try {
    if (userId && userId.trim()) {
      const userPrefs = parsePrefsJson(
        storage.getItem(sidebarStorageKey(userId)),
        viewportWidth
      );
      if (userPrefs) return userPrefs;
    }
    const browserPrefs = parsePrefsJson(
      storage.getItem(sidebarStorageKey(null)),
      viewportWidth
    );
    if (browserPrefs) return browserPrefs;
  } catch {
    return fallback;
  }

  return fallback;
}

export function saveSidebarPrefs(
  prefs: SidebarPrefs,
  userId?: string | null,
  viewportWidth = 1440,
  storage: StorageLike | null = getDefaultStorage()
): SidebarPrefs {
  const normalized: SidebarPrefs = {
    width: clampSidebarWidth(prefs.width, viewportWidth),
    collapsed: Boolean(prefs.collapsed),
  };
  if (!storage) return normalized;

  const serialized = JSON.stringify(normalized);
  try {
    storage.setItem(sidebarStorageKey(null), serialized);
    if (userId && userId.trim()) {
      storage.setItem(sidebarStorageKey(userId), serialized);
    }
  } catch (err) {
    console.debug("Sidebar preference save skipped:", err);
  }
  return normalized;
}
