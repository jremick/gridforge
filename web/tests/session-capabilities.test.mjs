import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../src/api.ts";
import { createInitialFormState } from "../src/presets.ts";
import {
  buildGoogleOAuthUrl,
  clampFormStateToCapabilities,
  classifyApiError,
  createDefaultNoneSession,
  extractAuthErrorFromSearch,
  extractInviteTokenFromSearch,
  formatAccountPill,
  formatUploadLimitLabel,
  isAtlasOptionAllowed,
  isGuestRenderPaused,
  isOutputAllowed,
} from "../src/session.ts";

function makeGuestSession(overrides = {}) {
  return {
    auth_mode: "hosted",
    authenticated: false,
    user: null,
    role: "guest",
    capabilities: {
      max_grid_cells: 1,
      max_grid_cols: 1,
      max_grid_rows: 1,
      strip_atlas_allowed: false,
      max_strip_sheets: 0,
      allowed_outputs: ["pdf"],
      packs_manage_allowed: false,
      max_upload_bytes: 5 * 1024 * 1024,
      max_dpi: 300,
      max_export_dpi: 300,
      turnstile_required: true,
      turnstile_sitekey: "0xTEST",
      google_oauth_enabled: true,
    },
    quotas: {
      daily_renders_limit: 2,
      daily_renders_used: 0,
      daily_renders_remaining: 2,
      hourly_previews_limit: 5,
      hourly_previews_used: 0,
      hourly_previews_remaining: 5,
      public_renders_paused: false,
      global_daily_public_limit: 50,
      global_daily_public_used: 12,
    },
    ...overrides,
  };
}

function makeFriendSession(overrides = {}) {
  return {
    auth_mode: "hosted",
    authenticated: true,
    user: {
      user_id: "usr-friend-1",
      email: "friend@example.com",
      display_name: "Trail Scout",
      role: "friend",
    },
    role: "friend",
    capabilities: {
      max_grid_cells: 9,
      max_grid_cols: 3,
      max_grid_rows: 3,
      strip_atlas_allowed: true,
      max_strip_sheets: 24,
      allowed_outputs: [
        "pdf",
        "geopdf",
        "mbtiles",
        "kmz",
        "gpkg_tiles",
        "osmand_sqlitedb",
        "field_bundle_zip",
      ],
      packs_manage_allowed: false,
      max_upload_bytes: 25 * 1024 * 1024,
      max_dpi: 600,
      max_export_dpi: 600,
      turnstile_required: false,
      turnstile_sitekey: null,
      google_oauth_enabled: true,
    },
    quotas: {
      daily_renders_limit: 20,
      daily_renders_used: 1,
      daily_renders_remaining: 19,
      hourly_previews_limit: 60,
      hourly_previews_used: 2,
      hourly_previews_remaining: 58,
      public_renders_paused: false,
      global_daily_public_limit: 50,
      global_daily_public_used: 12,
    },
    ...overrides,
  };
}

test("createDefaultNoneSession preserves unrestricted zero-login defaults", () => {
  const s = createDefaultNoneSession();
  assert.equal(s.auth_mode, "none");
  assert.equal(s.authenticated, false);
  assert.equal(s.role, "none");
  assert.equal(s.capabilities.max_grid_cells, 9);
  assert.equal(s.capabilities.strip_atlas_allowed, true);
  assert.equal(s.capabilities.packs_manage_allowed, true);
  assert.equal(s.capabilities.max_upload_bytes, 25 * 1024 * 1024);
  assert.equal(s.capabilities.turnstile_required, false);
  assert.ok(isAtlasOptionAllowed(3, 3, s.capabilities));
  assert.ok(isOutputAllowed("field_bundle_zip", s.capabilities));
});

test("clampFormStateToCapabilities clamps guest selections and preserves friend/none selections", () => {
  const guest = makeGuestSession();
  const state = createInitialFormState();
  state.grid_cols = 2;
  state.grid_rows = 2;
  state.include_atlas_index = true;
  state.stripPlan = { sheets: [{}] };
  state.outputs.pdf = false;
  state.outputs.geopdf = true;
  state.outputs.mbtiles = true;
  state.outputs.kmz = true;
  state.outputs.gpkg_tiles = true;
  state.outputs.osmand_sqlitedb = true;
  state.outputs.field_bundle_zip = true;
  state.dpi = 600;
  state.outputs.export_dpi = 450;
  state.overlayFile = { name: "big.gpx", size: 6 * 1024 * 1024 };

  const res = clampFormStateToCapabilities(state, guest.capabilities);
  assert.equal(res.changed, true);
  assert.equal(state.grid_cols, 1);
  assert.equal(state.grid_rows, 1);
  assert.equal(state.include_atlas_index, false);
  assert.equal(state.stripPlan, null);
  assert.equal(state.outputs.pdf, true);
  assert.equal(state.outputs.geopdf, false);
  assert.equal(state.outputs.mbtiles, false);
  assert.equal(state.outputs.kmz, false);
  assert.equal(state.outputs.gpkg_tiles, false);
  assert.equal(state.outputs.osmand_sqlitedb, false);
  assert.equal(state.outputs.field_bundle_zip, false);
  assert.equal(state.dpi, 300);
  assert.equal(state.outputs.export_dpi, 300);
  assert.equal(state.overlayFile, null);
  assert.ok(res.resetItems.includes("atlas grid"));
  assert.ok(res.resetItems.includes("strip atlas"));
  assert.ok(res.resetItems.includes("overlay file"));
  assert.ok(res.resetItems.includes("print DPI"));
  assert.ok(res.resetItems.includes("export DPI"));

  // Friend keeps 3x3, strip, digital exports, 600 DPI, and 10 MB overlay
  const friend = makeFriendSession();
  const fState = createInitialFormState();
  fState.grid_cols = 3;
  fState.grid_rows = 3;
  fState.dpi = 600;
  fState.outputs.export_dpi = 450;
  fState.include_atlas_index = true;
  fState.outputs.mbtiles = true;
  fState.outputs.field_bundle_zip = true;
  fState.overlayFile = { name: "route.gpx", size: 10 * 1024 * 1024 };
  const fRes = clampFormStateToCapabilities(fState, friend.capabilities);
  assert.equal(fRes.changed, false);
  assert.equal(fState.grid_cols, 3);
  assert.equal(fState.dpi, 600);
  assert.equal(fState.outputs.export_dpi, 450);
  assert.equal(fState.outputs.mbtiles, true);
  assert.ok(fState.overlayFile);
});

test("formatUploadLimitLabel formats whole MB cleanly and falls back to formatBytes", () => {
  assert.equal(formatUploadLimitLabel(25 * 1024 * 1024), "25 MB");
  assert.equal(formatUploadLimitLabel(5 * 1024 * 1024), "5 MB");
  assert.equal(formatUploadLimitLabel(512 * 1024), "512 KB");
});

test("formatAccountPill formats guest, friend, admin, exhausted, and paused states", () => {
  const guestPill = formatAccountPill(makeGuestSession());
  assert.equal(guestPill.roleLabel, "Guest");
  assert.equal(guestPill.quotaShort, "2/2");
  assert.equal(guestPill.quotaFull, "2/2 left");
  assert.equal(guestPill.exhausted, false);
  assert.equal(guestPill.paused, false);

  const guestExhausted = formatAccountPill(
    makeGuestSession({
      quotas: {
        daily_renders_limit: 2,
        daily_renders_used: 2,
        daily_renders_remaining: 0,
        hourly_previews_limit: 5,
        hourly_previews_used: 0,
        hourly_previews_remaining: 5,
        public_renders_paused: true,
        global_daily_public_limit: 50,
        global_daily_public_used: 50,
      },
    })
  );
  assert.equal(guestExhausted.exhausted, true);
  assert.equal(guestExhausted.paused, true);
  assert.equal(guestExhausted.quotaFull, "0/2 left");

  const friendPill = formatAccountPill(makeFriendSession());
  assert.equal(friendPill.roleLabel, "Friend");
  assert.equal(friendPill.quotaShort, "19/20");
  assert.match(friendPill.title, /Trail Scout/);
  assert.match(friendPill.title, /14d retention/);

  const adminPaused = formatAccountPill({
    ...makeFriendSession(),
    role: "admin",
    user: {
      user_id: "usr-admin-1",
      email: "admin@example.com",
      display_name: "Ops Lead",
      role: "admin",
    },
    quotas: {
      daily_renders_limit: null,
      daily_renders_used: 3,
      daily_renders_remaining: null,
      hourly_previews_limit: null,
      hourly_previews_used: 0,
      hourly_previews_remaining: null,
      public_renders_paused: true,
      global_daily_public_limit: 50,
      global_daily_public_used: 5,
    },
  });
  assert.equal(adminPaused.roleLabel, "Admin");
  assert.equal(adminPaused.paused, false);
  assert.equal(adminPaused.publicPaused, true);
  assert.match(adminPaused.title, /Unlimited/);
});

test("public_renders_paused kill switch pauses only guest role and leaves friend/admin/none unblocked", () => {
  const pausedGuest = makeGuestSession({
    quotas: {
      ...makeGuestSession().quotas,
      public_renders_paused: true,
    },
  });
  const guestPill = formatAccountPill(pausedGuest);
  assert.equal(guestPill.paused, true);
  assert.equal(guestPill.publicPaused, true);
  assert.match(guestPill.title, /Public renders paused/);
  assert.match(guestPill.ariaLabel, /, paused/);
  assert.equal(isGuestRenderPaused(pausedGuest), true);

  const pausedFriend = makeFriendSession({
    quotas: {
      ...makeFriendSession().quotas,
      public_renders_paused: true,
    },
  });
  const friendPill = formatAccountPill(pausedFriend);
  assert.equal(friendPill.paused, false);
  assert.equal(friendPill.publicPaused, true);
  assert.doesNotMatch(friendPill.title, /paused/i);
  assert.doesNotMatch(friendPill.ariaLabel, /paused/i);
  assert.equal(isGuestRenderPaused(pausedFriend), false);

  const pausedAdmin = {
    ...pausedFriend,
    role: "admin",
    user: {
      user_id: "usr-admin-1",
      email: "admin@example.com",
      display_name: "Ops Lead",
      role: "admin",
    },
  };
  const adminPill = formatAccountPill(pausedAdmin);
  assert.equal(adminPill.paused, false);
  assert.equal(adminPill.publicPaused, true);
  assert.equal(isGuestRenderPaused(pausedAdmin), false);

  const noneSession = createDefaultNoneSession();
  noneSession.quotas.public_renders_paused = true;
  const nonePill = formatAccountPill(noneSession);
  assert.equal(nonePill.paused, false);
  assert.equal(nonePill.publicPaused, false);
  assert.equal(isGuestRenderPaused(noneSession), false);
});

test("URL invite, auth error, and Google OAuth helpers extract and encode safely", () => {
  assert.equal(extractInviteTokenFromSearch("?invite=inv-abc-123"), "inv-abc-123");
  assert.equal(
    extractInviteTokenFromSearch("?center=-33,150&invite_token=inv-xyz"),
    "inv-xyz"
  );
  assert.equal(extractInviteTokenFromSearch("?invite=%20%20"), null);

  assert.equal(
    extractAuthErrorFromSearch("?auth_error=Invite%20expired"),
    "Invite expired"
  );
  assert.equal(extractAuthErrorFromSearch("?error=oauth_denied"), "oauth_denied");
  assert.equal(extractAuthErrorFromSearch("?center=-33,150"), null);

  assert.equal(buildGoogleOAuthUrl(), "/api/auth/google/start");
  assert.equal(
    buildGoogleOAuthUrl("tok+1/2"),
    "/api/auth/google/start?invite_token=tok%2B1%2F2"
  );
});

test("classifyApiError distinguishes 401, 403 Turnstile vs forbidden, 413, 429, and 503", () => {
  const e401 = classifyApiError(new ApiError("Auth required", 401));
  assert.equal(e401.kind, "auth");
  assert.equal(e401.shouldPromptSignIn, true);
  assert.equal(e401.shouldRefreshSession, true);

  const e403Turnstile = classifyApiError(
    new ApiError("Turnstile verification failed", 403)
  );
  assert.equal(e403Turnstile.kind, "turnstile");
  assert.equal(e403Turnstile.shouldResetTurnstile, true);
  assert.equal(e403Turnstile.shouldRefreshSession, false);

  const e403Tier = classifyApiError(
    new ApiError("Multi-sheet atlases require a Friend account", 403)
  );
  assert.equal(e403Tier.kind, "forbidden");
  assert.equal(e403Tier.shouldResetTurnstile, false);
  assert.equal(e403Tier.shouldRefreshSession, true);

  const e413 = classifyApiError(new ApiError("File too large", 413));
  assert.equal(e413.kind, "upload_too_large");

  const e429 = classifyApiError(new ApiError("Daily render limit reached", 429));
  assert.equal(e429.kind, "rate_limit");
  assert.equal(e429.shouldRefreshSession, true);

  const e503 = classifyApiError(
    new ApiError("Public renders paused", 503)
  );
  assert.equal(e503.kind, "service_unavailable");
  assert.equal(e503.shouldRefreshSession, true);
});
