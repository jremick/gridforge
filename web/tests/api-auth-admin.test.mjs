import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiError,
  acceptInvite,
  createAdminInvite,
  createShareSnapshot,
  deleteShareSnapshot,
  fetchAdminUsage,
  fetchAdminUsers,
  fetchBasemapInfo,
  fetchHealth,
  fetchPacks,
  fetchSession,
  fetchShareSnapshot,
  fetchShares,
  fetchRFLinkProfile,
  fetchRulerMetrics,
  fetchViewshedPreview,
  googleAuthStartUrl,
  loginWithPassword,
  logoutSession,
  revokeAdminInvite,
  submitJob,
  updateAdminSettings,
  updateAdminUser,
} from "../src/api.ts";

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function withMockFetch(handler, fn) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = handler;
  try {
    return await fn();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("auth session, login, logout, acceptInvite, and googleAuthStartUrl send expected requests", async () => {
  const calls = [];
  await withMockFetch(
    async (url, init = {}) => {
      calls.push({ url, init });
      return jsonResponse({ auth_mode: "hosted", authenticated: true, role: "friend" });
    },
    async () => {
      await fetchSession();
      await loginWithPassword({ email: "a@example.com", password: "secret" });
      await logoutSession();
      await acceptInvite({
        token: "inv-1",
        email: "a@example.com",
        password: "longpassword",
        display_name: "Tester",
      });
    }
  );

  assert.equal(calls.length, 4);
  assert.equal(calls[0].url, "/api/auth/session");
  assert.equal(calls[0].init.credentials, "same-origin");

  assert.equal(calls[1].url, "/api/auth/login");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.credentials, "same-origin");
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    email: "a@example.com",
    password: "secret",
  });

  assert.equal(calls[2].url, "/api/auth/logout");
  assert.equal(calls[2].init.method, "POST");
  assert.equal(calls[2].init.credentials, "same-origin");

  assert.equal(calls[3].url, "/api/auth/accept-invite");
  assert.equal(calls[3].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[3].init.body), {
    token: "inv-1",
    email: "a@example.com",
    password: "longpassword",
    display_name: "Tester",
  });

  assert.equal(googleAuthStartUrl(), "/api/auth/google/start");
  assert.equal(
    googleAuthStartUrl(" inv 2 "),
    "/api/auth/google/start?invite_token=inv%202"
  );
});

test("submitJob and fetchViewshedPreview attach X-Turnstile-Token only when provided", async () => {
  const calls = [];
  await withMockFetch(
    async (url, init = {}) => {
      calls.push({ url, init });
      return jsonResponse({ ok: true });
    },
    async () => {
      const dummyPayload = { center: "-33.7, 150.3", scale: 25000 };
      await submitJob(dummyPayload, null, "tok-job-1");
      await submitJob(dummyPayload, null, null);
      await fetchViewshedPreview(
        {
          label: "OBS1",
          lat: -33.7,
          lon: 150.3,
          observer_height_m: 2,
          target_height_m: 1.5,
          max_distance_m: 10000,
        },
        "auto",
        null,
        undefined,
        "tok-vs-1"
      );
      await fetchViewshedPreview(
        {
          label: "OBS1",
          lat: -33.7,
          lon: 150.3,
          observer_height_m: 2,
          target_height_m: 1.5,
          max_distance_m: 10000,
        },
        "auto",
        null,
        undefined,
        "   "
      );
    }
  );

  assert.equal(calls[0].url, "/api/jobs");
  assert.equal(calls[0].init.headers["X-Turnstile-Token"], "tok-job-1");
  assert.equal(calls[0].init.headers["Content-Type"], undefined);
  assert.ok(calls[0].init.body instanceof FormData);

  assert.equal(calls[1].url, "/api/jobs");
  assert.equal(calls[1].init.headers, undefined);

  assert.equal(calls[2].url, "/api/viewshed-preview");
  assert.equal(calls[2].init.headers["X-Turnstile-Token"], "tok-vs-1");
  assert.equal(calls[2].init.headers["Content-Type"], "application/json");

  assert.equal(calls[3].url, "/api/viewshed-preview");
  assert.equal(calls[3].init.headers["X-Turnstile-Token"], undefined);
});

test("fetchRFLinkProfile and fetchRulerMetrics support X-Turnstile-Token and nullable dem_source", async () => {
  const calls = [];
  await withMockFetch(
    async (url, init = {}) => {
      calls.push({ url, init });
      return jsonResponse({ ok: true });
    },
    async () => {
      await fetchRFLinkProfile(
        {
          from_lat: -33.7,
          from_lon: 150.3,
          to_lat: -33.71,
          to_lon: 150.31,
          from_height_m: 2,
          to_height_m: 2,
          freq_mhz: 915,
          k_factor: 1.333,
          from_label: "A",
          to_label: "B",
        },
        "auto",
        null,
        undefined,
        "tok-rf-1"
      );
      await fetchRulerMetrics(
        [
          { lat: -33.7, lon: 150.3 },
          { lat: -33.71, lon: 150.31 },
        ],
        null,
        null,
        undefined,
        undefined
      );
      await fetchRulerMetrics(
        [
          { lat: -33.7, lon: 150.3 },
          { lat: -33.71, lon: 150.31 },
        ],
        "auto",
        null,
        undefined,
        "tok-ruler-1"
      );
    }
  );

  assert.equal(calls[0].url, "/api/rf-link-profile");
  assert.equal(calls[0].init.headers["X-Turnstile-Token"], "tok-rf-1");

  assert.equal(calls[1].url, "/api/ruler-metrics");
  assert.equal(calls[1].init.headers["X-Turnstile-Token"], undefined);
  assert.equal(JSON.parse(calls[1].init.body).dem_source, null);

  assert.equal(calls[2].url, "/api/ruler-metrics");
  assert.equal(calls[2].init.headers["X-Turnstile-Token"], "tok-ruler-1");
  assert.equal(JSON.parse(calls[2].init.body).dem_source, "auto");
});

test("admin API endpoints hit expected paths, methods, and payloads", async () => {
  const calls = [];
  await withMockFetch(
    async (url, init = {}) => {
      calls.push({ url, init });
      return jsonResponse({ ok: true }, url.endsWith("/invites") && init.method === "POST" ? 201 : 200);
    },
    async () => {
      await fetchAdminUsage();
      await fetchAdminUsers();
      await createAdminInvite({ email: "f@example.com", role: "friend", expires_hours: 48 });
      await revokeAdminInvite("inv/1");
      await updateAdminUser("usr 2", { disabled: true, daily_render_limit: 5 });
      await updateAdminSettings({ public_renders_paused: true });
    }
  );

  assert.equal(calls[0].url, "/api/admin/usage");
  assert.equal(calls[1].url, "/api/admin/users");
  assert.equal(calls[2].url, "/api/admin/invites");
  assert.equal(calls[2].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[2].init.body), {
    email: "f@example.com",
    role: "friend",
    expires_hours: 48,
  });

  assert.equal(calls[3].url, "/api/admin/invites/inv%2F1");
  assert.equal(calls[3].init.method, "DELETE");

  assert.equal(calls[4].url, "/api/admin/users/usr%202");
  assert.equal(calls[4].init.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[4].init.body), {
    disabled: true,
    daily_render_limit: 5,
  });

  assert.equal(calls[5].url, "/api/admin/settings");
  assert.equal(calls[5].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[5].init.body), {
    public_renders_paused: true,
  });
});

test("share snapshot API helpers hit expected paths, methods, and payloads", async () => {
  const calls = [];
  await withMockFetch(
    async (url, init = {}) => {
      calls.push({ url, init });
      if (url === "/api/shares" && init.method === "POST") {
        return jsonResponse(
          {
            share_id: "AbCdEf1234567890",
            share_path: "/?share=AbCdEf1234567890",
            name: "Trip",
            notes: "",
            state: { center: "-33.7,150.3" },
            view_count: 0,
            created_at: "2026-11-27T00:00:00Z",
            expires_at: null,
          },
          201
        );
      }
      if (url === "/api/shares") {
        return jsonResponse({ shares: [] });
      }
      if (init.method === "DELETE") {
        return jsonResponse({ deleted: true });
      }
      return jsonResponse({
        share_id: "AbCdEf1234567890",
        share_path: "/?share=AbCdEf1234567890",
        name: "Trip",
        notes: "",
        state: { center: "-33.7,150.3" },
        view_count: 1,
        created_at: "2026-11-27T00:00:00Z",
        expires_at: null,
      });
    },
    async () => {
      await createShareSnapshot({ name: "Trip", state: { center: "-33.7,150.3" } });
      await createShareSnapshot({ project_id: "proj_12345678" });
      await fetchShareSnapshot("AbCdEf1234567890");
      await fetchShares();
      await deleteShareSnapshot("AbCdEf1234567890");
    }
  );

  assert.equal(calls[0].url, "/api/shares");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    name: "Trip",
    state: { center: "-33.7,150.3" },
  });

  assert.equal(calls[1].url, "/api/shares");
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    project_id: "proj_12345678",
  });

  assert.equal(calls[2].url, "/api/shares/AbCdEf1234567890");
  assert.equal(calls[3].url, "/api/shares");
  assert.equal(calls[4].url, "/api/shares/AbCdEf1234567890");
  assert.equal(calls[4].init.method, "DELETE");
});

test("ApiError preserves HTTP status and parses FastAPI JSON and plain-text details", async () => {
  for (const [status, body, expectedMsg] of [
    [401, jsonResponse({ detail: "Invalid email or password" }, 401), "Invalid email or password"],
    [
      403,
      new Response("Cross-origin writes are disabled", {
        status: 403,
        headers: { "content-type": "text/plain" },
      }),
      "Cross-origin writes are disabled",
    ],
    [
      413,
      jsonResponse({ detail: [{ loc: ["body", "overlay_file"], msg: "Exceeds 5 MB" }] }, 413),
      "body.overlay_file: Exceeds 5 MB",
    ],
    [429, jsonResponse({ detail: "Daily quota exceeded" }, 429), "Daily quota exceeded"],
    [503, jsonResponse({ detail: "Public renders paused" }, 503), "Public renders paused"],
  ]) {
    await withMockFetch(
      async () => body,
      async () => {
        await assert.rejects(
          () => fetchSession(),
          (err) => {
            assert.ok(err instanceof ApiError);
            assert.equal(err.status, status);
            assert.equal(err.message, expectedMsg);
            return true;
          }
        );
      }
    );
  }
});

test("ApiError preserves non-standard JSON error bodies without stream double-read errors", async () => {
  for (const [status, resFactory, expected] of [
    [
      403,
      () => jsonResponse({ detail: { code: "E1", reason: "Blocked" } }, 403),
      '{"code":"E1","reason":"Blocked"}',
    ],
    [
      403,
      () => jsonResponse({ error: "Denied" }, 403),
      "Denied",
    ],
    [
      429,
      () => jsonResponse({ custom: 42 }, 429),
      '{"custom":42}',
    ],
    [
      502,
      () =>
        new Response("Bad Gateway upstream", {
          status: 502,
          headers: { "content-type": "application/json" },
        }),
      "Bad Gateway upstream",
    ],
  ]) {
    await withMockFetch(
      async () => resFactory(),
      async () => {
        await assert.rejects(
          () => fetchSession(),
          (err) => {
            assert.ok(err instanceof ApiError);
            assert.equal(err.status, status);
            assert.equal(err.message, expected);
            return true;
          }
        );
      }
    );
  }
});

test("fetchBasemapInfo, fetchPacks, and fetchHealth return payloads and attach bounded AbortSignals", async () => {
  const calls = [];
  const controller = new AbortController();
  await withMockFetch(
    async (url, init = {}) => {
      calls.push({ url, init });
      if (url === "/api/health") return jsonResponse({ status: "ok", queue_depth: 0 });
      if (url.startsWith("/api/basemap/info")) {
        return jsonResponse({ available: true, pack_id: "nsw-blue" });
      }
      return jsonResponse({ packs: [{ pack_id: "nsw-blue", name: "Blue Mountains" }] });
    },
    async () => {
      const health = await fetchHealth();
      const baseDefault = await fetchBasemapInfo(null, controller.signal);
      const basePack = await fetchBasemapInfo("pack 1", controller.signal);
      const packs = await fetchPacks(controller.signal);

      assert.equal(health.status, "ok");
      assert.equal(baseDefault.available, true);
      assert.equal(basePack.pack_id, "nsw-blue");
      assert.equal(packs.packs.length, 1);
      assert.equal(packs.packs[0].pack_id, "nsw-blue");
    }
  );

  assert.equal(calls[0].url, "/api/health");
  assert.ok(calls[0].init.signal instanceof AbortSignal);
  assert.equal(calls[0].init.signal.aborted, false);

  assert.equal(calls[1].url, "/api/basemap/info");
  assert.equal(calls[2].url, "/api/basemap/info?pack_id=pack%201");
  assert.equal(calls[3].url, "/api/packs");
  assert.equal(calls[3].init.signal.aborted, false);
  controller.abort();
  assert.equal(calls[3].init.signal.aborted, true);

  // Fallback when AbortSignal.any is unavailable: callerSignal is passed directly
  const origAny = AbortSignal.any;
  const fallbackCtrl = new AbortController();
  let seenFallbackSignal = null;
  try {
    AbortSignal.any = undefined;
    await withMockFetch(
      async (_url, init = {}) => {
        seenFallbackSignal = init.signal;
        return jsonResponse({ packs: [] });
      },
      async () => {
        await fetchPacks(fallbackCtrl.signal);
      }
    );
    assert.equal(seenFallbackSignal, fallbackCtrl.signal);
  } finally {
    AbortSignal.any = origAny;
  }
});
