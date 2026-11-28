import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  clampSidebarWidth,
  defaultSidebarCollapsedForViewport,
  getMaxSidebarWidthForViewport,
  loadSidebarPrefs,
  saveSidebarPrefs,
  sidebarStorageKey,
} from "../src/sidebarPrefs.ts";
import { loadStudioTheme, saveStudioTheme } from "../src/themePrefs.ts";

function makeMemoryStorage() {
  const map = new Map();
  return {
    getItem(k) {
      return map.has(k) ? map.get(k) : null;
    },
    setItem(k, v) {
      map.set(k, String(v));
    },
    _map: map,
  };
}

test("clampSidebarWidth enforces practical minimum, maximum, and viewport ratio caps", () => {
  assert.equal(clampSidebarWidth(120, 1440), MIN_SIDEBAR_WIDTH);
  assert.equal(clampSidebarWidth(368, 1440), DEFAULT_SIDEBAR_WIDTH);
  assert.equal(clampSidebarWidth(900, 1440), MAX_SIDEBAR_WIDTH);
  assert.equal(clampSidebarWidth(NaN, 1440), DEFAULT_SIDEBAR_WIDTH);

  // On a 900px viewport, 55% = 495px caps below 560px
  assert.equal(getMaxSidebarWidthForViewport(900), 495);
  assert.equal(clampSidebarWidth(540, 900), 495);

  // On a 390px phone viewport, mobile cap bounds cleanly <= 360px
  assert.equal(getMaxSidebarWidthForViewport(390), 360);
  assert.equal(clampSidebarWidth(420, 390), 360);
});

test("loadSidebarPrefs and saveSidebarPrefs persist per-user and browser preferences safely", () => {
  const store = makeMemoryStorage();

  // Default fallback when empty
  assert.deepEqual(loadSidebarPrefs(null, 1440, store), {
    width: DEFAULT_SIDEBAR_WIDTH,
    collapsed: false,
  });

  // Save browser preference (guest/local)
  saveSidebarPrefs({ width: 420, collapsed: true }, null, 1440, store);
  assert.deepEqual(loadSidebarPrefs(null, 1440, store), {
    width: 420,
    collapsed: true,
  });

  // Signed-in user A inherits browser pref until they save their own
  assert.deepEqual(loadSidebarPrefs("usr-a", 1440, store), {
    width: 420,
    collapsed: true,
  });

  // User A saves custom width & expanded state
  saveSidebarPrefs({ width: 480, collapsed: false }, "usr-a", 1440, store);
  assert.deepEqual(loadSidebarPrefs("usr-a", 1440, store), {
    width: 480,
    collapsed: false,
  });

  // User B saves narrower width & collapsed state without clobbering User A's key
  saveSidebarPrefs({ width: 312, collapsed: true }, "usr-b", 1440, store);
  assert.deepEqual(loadSidebarPrefs("usr-b", 1440, store), {
    width: 312,
    collapsed: true,
  });
  assert.deepEqual(loadSidebarPrefs("usr-a", 1440, store), {
    width: 480,
    collapsed: false,
  });

  // Corrupted JSON falls back cleanly without throwing
  store.setItem(sidebarStorageKey("usr-bad"), "{not-json");
  assert.deepEqual(loadSidebarPrefs("usr-bad", 1440, store), {
    width: 312,
    collapsed: true,
  });

  // Throwing storage (private mode / quota exceeded) degrades gracefully
  const brokenStorage = {
    getItem() {
      throw new Error("SecurityError");
    },
    setItem() {
      throw new Error("QuotaExceededError");
    },
  };
  assert.deepEqual(loadSidebarPrefs("usr-a", 1440, brokenStorage), {
    width: DEFAULT_SIDEBAR_WIDTH,
    collapsed: false,
  });
  assert.deepEqual(
    saveSidebarPrefs({ width: 200, collapsed: true }, "usr-a", 1440, brokenStorage),
    { width: MIN_SIDEBAR_WIDTH, collapsed: true }
  );

  // Mobile viewports (<= 680px) default to collapsed so first-time visitors see the live map
  const emptyMobileStore = makeMemoryStorage();
  assert.equal(defaultSidebarCollapsedForViewport(390), true);
  assert.equal(defaultSidebarCollapsedForViewport(1440), false);
  assert.deepEqual(loadSidebarPrefs(null, 390, emptyMobileStore), {
    width: 360,
    collapsed: true,
  });
});

test("loadStudioTheme and saveStudioTheme default to dark and persist daylight selection safely", () => {
  const store = makeMemoryStorage();
  assert.equal(loadStudioTheme(store), "dark");
  assert.equal(saveStudioTheme("light", store), "light");
  assert.equal(loadStudioTheme(store), "light");
  assert.equal(saveStudioTheme("dark", store), "dark");
  assert.equal(loadStudioTheme(store), "dark");

  store.setItem("gridforge.ui.theme.v1", "invalid");
  assert.equal(loadStudioTheme(store), "dark");
});
