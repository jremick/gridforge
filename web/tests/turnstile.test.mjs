import assert from "node:assert/strict";
import test from "node:test";

import { createDefaultNoneSession } from "../src/session.ts";
import { TurnstileController, defaultLoadTurnstileApi } from "../src/turnstile.ts";

function makeFakeContainer() {
  const classes = new Set(["hidden"]);
  return {
    classList: {
      toggle(name, force) {
        if (force === undefined) {
          if (classes.has(name)) classes.delete(name);
          else classes.add(name);
          return classes.has(name);
        }
        if (force) classes.add(name);
        else classes.delete(name);
        return force;
      },
      contains(name) {
        return classes.has(name);
      },
    },
  };
}

function makeFakeTurnstileApi() {
  let seq = 1;
  let lastOptions = null;
  let currentResponse = undefined;
  const calls = { render: 0, reset: 0, remove: 0 };

  const api = {
    render(_container, options) {
      calls.render += 1;
      lastOptions = options;
      currentResponse = undefined;
      return `widget-${seq++}`;
    },
    reset() {
      calls.reset += 1;
      currentResponse = undefined;
    },
    remove() {
      calls.remove += 1;
      lastOptions = null;
      currentResponse = undefined;
    },
    getResponse() {
      return currentResponse;
    },
    solve(token) {
      currentResponse = token;
      lastOptions?.callback?.(token);
    },
    expire() {
      currentResponse = undefined;
      lastOptions?.["expired-callback"]?.();
    },
    fail(code) {
      currentResponse = undefined;
      lastOptions?.["error-callback"]?.(code);
    },
    getLastOptions() {
      return lastOptions;
    },
  };

  return { api, calls };
}

function guestSession(sitekey = "0xSITEKEY") {
  const base = createDefaultNoneSession();
  return {
    ...base,
    auth_mode: "hosted",
    role: "guest",
    capabilities: {
      ...base.capabilities,
      turnstile_required: true,
      turnstile_sitekey: sitekey,
    },
  };
}

test("TurnstileController is a no-op when auth_mode is none or user is friend/admin", async () => {
  let loaderCalls = 0;
  const container = makeFakeContainer();
  const { api, calls } = makeFakeTurnstileApi();
  const ctrl = new TurnstileController({
    container,
    loadApi: async () => {
      loaderCalls += 1;
      return api;
    },
  });

  await ctrl.syncSession(createDefaultNoneSession());
  assert.equal(loaderCalls, 0);
  assert.equal(calls.render, 0);
  assert.equal(ctrl.isActive(), false);
  assert.equal(await ctrl.acquireToken(), null);
  assert.equal(container.classList.contains("hidden"), true);

  const friend = {
    ...createDefaultNoneSession(),
    auth_mode: "hosted",
    authenticated: true,
    role: "friend",
  };
  await ctrl.syncSession(friend);
  assert.equal(loaderCalls, 0);
  assert.equal(await ctrl.acquireToken(), null);
});

test("TurnstileController renders explicitly once, enforces single-use reset, and handles login unmount", async () => {
  const container = makeFakeContainer();
  const { api, calls } = makeFakeTurnstileApi();
  let interactiveCount = 0;

  const ctrl = new TurnstileController({
    container,
    loadApi: async () => api,
    onInteractiveNeeded: () => {
      interactiveCount += 1;
    },
  });

  const session = guestSession("0xPUBLIC");
  await ctrl.syncSession(session);
  await ctrl.syncSession(session);

  assert.equal(calls.render, 1);
  assert.equal(ctrl.isActive(), true);
  assert.equal(container.classList.contains("hidden"), false);
  assert.equal(api.getLastOptions().sitekey, "0xPUBLIC");
  assert.equal(api.getLastOptions().theme, "dark");
  assert.equal(api.getLastOptions().execution, "render");
  assert.equal(api.getLastOptions().appearance, "interaction-only");

  // Pre-solved token returns immediately without triggering interactive prompt
  api.solve("tok-1");
  const first = await ctrl.acquireToken();
  assert.equal(first, "tok-1");
  assert.equal(interactiveCount, 0);

  // Token cannot be reused; explicit reset() prepares widget for next solve()
  ctrl.reset();
  assert.equal(calls.reset, 1);
  const secondPromise = ctrl.acquireToken(500);
  await Promise.resolve();
  assert.equal(interactiveCount, 1);

  api.solve("tok-2");
  const second = await secondPromise;
  assert.equal(second, "tok-2");

  // Unmount on login removes widget and hides container
  await ctrl.syncSession({
    ...session,
    authenticated: true,
    role: "friend",
    capabilities: {
      ...session.capabilities,
      turnstile_required: false,
      turnstile_sitekey: null,
    },
  });
  assert.equal(calls.remove, 1);
  assert.equal(ctrl.isActive(), false);
  assert.equal(container.classList.contains("hidden"), true);
  assert.equal(await ctrl.acquireToken(), null);
});

test("TurnstileController handles expired-callback and error-callback cleanly", async () => {
  const container = makeFakeContainer();
  const { api, calls } = makeFakeTurnstileApi();
  const ctrl = new TurnstileController({
    container,
    loadApi: async () => api,
  });

  await ctrl.syncSession(guestSession());
  api.solve("tok-stale");
  api.expire();
  assert.equal(calls.reset, 1);

  const pending = ctrl.acquireToken(500);
  await new Promise((r) => setTimeout(r, 10));
  api.fail("300010");

  await assert.rejects(pending, /300010/);
  assert.match(ctrl.getLastError(), /300010/);
});

test("TurnstileController.syncSession handles loadApi() failure without rejecting and recovers on retry", async () => {
  const container = makeFakeContainer();
  const { api, calls } = makeFakeTurnstileApi();
  let attempt = 0;

  const ctrl = new TurnstileController({
    container,
    loadApi: async () => {
      attempt += 1;
      if (attempt === 1) {
        throw new Error("Security verification could not load (blocked or offline).");
      }
      return api;
    },
  });

  // Must not reject even when CDN/script loading fails during bootstrap
  await ctrl.syncSession(guestSession("0xRETRY"));
  assert.equal(ctrl.isActive(), true);
  assert.equal(ctrl.getWidgetId(), null);
  assert.match(ctrl.getLastError(), /blocked or offline/);

  // Subsequent acquireToken() retries loadApi(), renders widget, and clears lastError
  const pending = ctrl.acquireToken(500);
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(calls.render, 1);
  assert.equal(ctrl.getLastError(), null);
  api.solve("tok-recovered");
  assert.equal(await pending, "tok-recovered");
});

test("TurnstileController.acquireToken handles synchronous callback during lastError recovery reset", async () => {
  const container = makeFakeContainer();
  const { api, calls } = makeFakeTurnstileApi();
  const origReset = api.reset;
  let syncOnReset = false;
  api.reset = () => {
    origReset();
    if (syncOnReset) {
      api.solve("sync-tok-1");
    }
  };

  const ctrl = new TurnstileController({
    container,
    loadApi: async () => api,
  });

  await ctrl.syncSession(guestSession());
  const firstAttempt = ctrl.acquireToken(500);
  await new Promise((r) => setTimeout(r, 10));
  api.fail("600010");
  await assert.rejects(firstAttempt, /600010/);
  assert.match(ctrl.getLastError(), /600010/);

  syncOnReset = true;
  const tok = await ctrl.acquireToken(120);
  assert.equal(tok, "sync-tok-1");
  assert.equal(calls.reset, 1);
  assert.equal(ctrl.getLastError(), null);

  // Consumed token must not be re-served
  syncOnReset = false;
  const next = ctrl.acquireToken(40);
  await assert.rejects(next, /timed out/);
});

test("defaultLoadTurnstileApi removes failed and timed-out script tags so retries insert a fresh script and succeed", async () => {
  const origWindow = globalThis.window;
  const origDocument = globalThis.document;

  const scripts = [];
  const head = {
    appendChild(el) {
      el.parentNode = head;
      scripts.push(el);
      return el;
    },
    removeChild(el) {
      const idx = scripts.indexOf(el);
      if (idx >= 0) scripts.splice(idx, 1);
      el.parentNode = null;
    },
  };

  globalThis.document = {
    head,
    documentElement: head,
    createElement(tag) {
      assert.equal(tag, "script");
      const listeners = new Map();
      const el = {
        src: "",
        async: false,
        defer: false,
        dataset: {},
        parentNode: null,
        addEventListener(type, fn) {
          listeners.set(type, fn);
        },
        remove() {
          head.removeChild(el);
        },
        dispatchError() {
          listeners.get("error")?.();
        },
      };
      return el;
    },
    querySelector(selector) {
      if (selector === 'script[data-gf-turnstile="1"]') {
        return scripts.find((s) => s.dataset.gfTurnstile === "1") ?? null;
      }
      return null;
    },
  };

  globalThis.window = {
    setTimeout,
    clearTimeout,
  };

  try {
    // 1. Script error event removes dead <script> so retry inserts a fresh one
    const firstErrLoad = defaultLoadTurnstileApi(500);
    assert.equal(scripts.length, 1);
    const failedScript = scripts[0];
    failedScript.dispatchError();
    await assert.rejects(firstErrLoad, /blocked or offline/);
    assert.equal(scripts.length, 0);

    // 2. Timeout also removes dead <script>
    const timeoutLoad = defaultLoadTurnstileApi(25);
    assert.equal(scripts.length, 1);
    const timedOutScript = scripts[0];
    await assert.rejects(timeoutLoad, /timed out/);
    assert.equal(scripts.length, 0);

    // 3. Subsequent retry inserts a new <script> and resolves once window.turnstile + __gfTurnstileReady fire
    const { api } = makeFakeTurnstileApi();
    const retryLoad = defaultLoadTurnstileApi(500);
    assert.equal(scripts.length, 1);
    assert.notEqual(scripts[0], failedScript);
    assert.notEqual(scripts[0], timedOutScript);

    globalThis.window.turnstile = api;
    globalThis.window.__gfTurnstileReady?.();
    const resolved = await retryLoad;
    assert.equal(resolved, api);
  } finally {
    globalThis.window = origWindow;
    globalThis.document = origDocument;
  }
});

test("TurnstileController resets widget automatically when acquireToken is called after a consumed token", async () => {
  const container = makeFakeContainer();
  let resetCalls = 0;
  let seq = 0;
  let opts = null;
  const api = {
    render(_container, options) {
      opts = options;
      seq += 1;
      options.callback?.(`tok-${seq}`);
      return "widget-auto";
    },
    reset() {
      resetCalls += 1;
      seq += 1;
      opts?.callback?.(`tok-${seq}`);
    },
    remove() {},
    getResponse() {
      return undefined;
    },
  };

  const ctrl = new TurnstileController({
    container,
    loadApi: async () => api,
  });

  await ctrl.syncSession(guestSession("0xAUTO"));
  const t1 = await ctrl.acquireToken(200);
  const t2 = await ctrl.acquireToken(200);
  assert.equal(t1, "tok-1");
  assert.equal(t2, "tok-2");
  assert.equal(resetCalls, 1);

  // Also works when consecutive acquireToken() calls are queued concurrently and reset triggers async solve
  let asyncResetCalls = 0;
  let asyncOpts = null;
  const asyncApi = {
    render(_container, options) {
      asyncOpts = options;
      return "widget-async";
    },
    reset() {
      asyncResetCalls += 1;
      queueMicrotask(() => asyncOpts?.callback?.(`async-${asyncResetCalls}`));
    },
    remove() {},
    getResponse() {
      return undefined;
    },
  };
  const asyncCtrl = new TurnstileController({
    container: makeFakeContainer(),
    loadApi: async () => asyncApi,
  });
  await asyncCtrl.syncSession(guestSession("0xASYNC"));
  const p1 = asyncCtrl.acquireToken(200);
  const p2 = asyncCtrl.acquireToken(200);
  asyncOpts?.callback?.("async-0");
  const [a1, a2] = await Promise.all([p1, p2]);
  assert.equal(a1, "async-0");
  assert.equal(a2, "async-1");
  assert.equal(asyncResetCalls, 1);
});

test("TurnstileController.reset() does not clobber an active pendingWaiter from an overlapping acquireToken()", async () => {
  const container = makeFakeContainer();
  let resetCalls = 0;
  let opts = null;
  const api = {
    render(_container, options) {
      opts = options;
      options.callback?.("tok-1");
      return "widget-overlap";
    },
    reset() {
      resetCalls += 1;
      const callIdx = resetCalls;
      setTimeout(() => opts?.callback?.(`tok-reset-${callIdx}`), 30);
    },
    remove() {},
    getResponse() {
      return undefined;
    },
  };

  const ctrl = new TurnstileController({
    container,
    loadApi: async () => api,
  });

  await ctrl.syncSession(guestSession("0xOVERLAP"));
  const t1 = await ctrl.acquireToken(500);
  assert.equal(t1, "tok-1");

  const p2 = ctrl.acquireToken(500);
  await new Promise((r) => setTimeout(r, 10));
  // Simulated finally { this.turnstile.reset(); } from request 1 while request 2 is waiting
  ctrl.reset();

  const t2 = await p2;
  assert.equal(t2, "tok-reset-1");
  assert.equal(resetCalls, 1);
});

test("TurnstileController ignores stale callbacks and queued acquires across unmount and re-sync", async () => {
  const container = makeFakeContainer();
  const capturedCallbacks = [];
  const api = {
    render(_container, options) {
      capturedCallbacks.push(options);
      return `widget-${capturedCallbacks.length}`;
    },
    reset() {},
    remove() {},
    getResponse() {
      return undefined;
    },
  };

  const ctrl = new TurnstileController({
    container,
    loadApi: async () => api,
  });

  await ctrl.syncSession(guestSession("0xGEN"));
  const firstOpts = capturedCallbacks[0];
  const pending = ctrl.acquireToken(500);
  const queued = ctrl.acquireToken(500);

  // Tenant reset unmounts widget and advances generation, then next guest re-syncs
  ctrl.unmount();
  await ctrl.syncSession(guestSession("0xGEN"));

  // Both pre-unmount acquires resolve cleanly to null (not a new guest's token)
  assert.equal(await pending, null);
  assert.equal(await queued, null);

  // A delayed callback from the old widget must not populate the new generation's cache or errors
  firstOpts.callback("stale-token-from-old-tenant");
  firstOpts["error-callback"]("999999");
  assert.equal(ctrl.getLastError(), null);

  const freshPending = ctrl.acquireToken(200);
  await new Promise((r) => setTimeout(r, 10));
  capturedCallbacks[1].callback("fresh-token-new-tenant");
  assert.equal(await freshPending, "fresh-token-new-tenant");
});
