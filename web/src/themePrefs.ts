export type StudioTheme = "dark" | "light";

const STORAGE_KEY = "gridforge.ui.theme.v1";

export interface ThemeStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function getDefaultStorage(): ThemeStorageLike | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      return window.localStorage;
    }
  } catch {
    return null;
  }
  return null;
}

export function loadStudioTheme(
  storage: ThemeStorageLike | null = getDefaultStorage()
): StudioTheme {
  if (!storage) return "dark";
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (raw === "light" || raw === "dark") {
      return raw;
    }
  } catch {
    return "dark";
  }
  return "dark";
}

export function saveStudioTheme(
  theme: StudioTheme,
  storage: ThemeStorageLike | null = getDefaultStorage()
): StudioTheme {
  if (!storage) return theme;
  try {
    storage.setItem(STORAGE_KEY, theme);
  } catch (err) {
    console.debug("Theme preference save skipped:", err);
  }
  return theme;
}

export function applyStudioTheme(theme: StudioTheme): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
}
