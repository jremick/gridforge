import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import * as esbuild from "../node_modules/esbuild/lib/main.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, "..");

function decodeEntities(raw) {
  return raw
    .replaceAll("&amp;", "&")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&#x27;", "'");
}

function splitSelectors(selector) {
  const parts = [];
  let cur = "";
  let depth = 0;
  let quote = null;
  for (const ch of selector) {
    if (quote) {
      cur += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      cur += ch;
    } else if (ch === "[" || ch === "(") {
      depth += 1;
      cur += ch;
    } else if (ch === "]" || ch === ")") {
      depth = Math.max(0, depth - 1);
      cur += ch;
    } else if (ch === "," && depth === 0) {
      if (cur.trim()) parts.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  if (cur.trim()) parts.push(cur.trim());
  return parts;
}

function matchesSingleCompound(el, compound) {
  if (!el || el.nodeType !== 1) return false;
  let rest = compound.trim();
  if (!rest) return false;

  // Handle :not(...) clauses cleanly
  while (true) {
    const notIdx = rest.indexOf(":not(");
    if (notIdx < 0) break;
    let depth = 0;
    let end = -1;
    for (let i = notIdx + 4; i < rest.length; i++) {
      if (rest[i] === "(") depth += 1;
      else if (rest[i] === ")") {
        depth -= 1;
        if (depth === 0) {
          end = i;
          break;
        }
      }
    }
    if (end < 0) break;
    const inner = rest.slice(notIdx + 5, end).trim();
    if (matchesSingleCompound(el, inner)) return false;
    rest = (rest.slice(0, notIdx) + rest.slice(end + 1)).trim();
  }

  if (rest.endsWith(":disabled")) {
    if (!el.disabled) return false;
    rest = rest.slice(0, -":disabled".length);
  }

  if (!rest) return true;

  // Attribute selectors [attr] or [attr="val"]
  const attrMatches = [];
  rest = rest.replace(/\[([^\]]+)\]/g, (_m, inner) => {
    attrMatches.push(inner.trim());
    return "";
  });

  for (const attrExpr of attrMatches) {
    const eqIdx = attrExpr.indexOf("=");
    if (eqIdx < 0) {
      if (!el.hasAttribute(attrExpr)) return false;
    } else {
      const k = attrExpr.slice(0, eqIdx).trim();
      const v = attrExpr.slice(eqIdx + 1).trim().replace(/^['"]|['"]$/g, "");
      if (el.getAttribute(k) !== v) return false;
    }
  }

  // Tag, #id, .class tokens
  const idMatch = rest.match(/#([A-Za-z0-9_-]+)/);
  if (idMatch && el.id !== idMatch[1]) return false;
  rest = rest.replace(/#[A-Za-z0-9_-]+/g, "");

  const clsMatches = [...rest.matchAll(/\.([A-Za-z0-9_-]+)/g)].map((m) => m[1]);
  for (const c of clsMatches) {
    if (!el.classList.contains(c)) return false;
  }
  rest = rest.replace(/\.[A-Za-z0-9_-]+/g, "").trim();

  if (rest && rest !== "*") {
    if (el.tagName.toLowerCase() !== rest.toLowerCase()) return false;
  }
  return true;
}

function matchesSelector(el, selectorList) {
  for (const sel of splitSelectors(selectorList)) {
    const tokens = sel.trim().split(/\s+/);
    if (tokens.length === 1) {
      if (matchesSingleCompound(el, tokens[0])) return true;
    } else {
      // Descendant combinator
      const last = tokens[tokens.length - 1];
      if (!matchesSingleCompound(el, last)) continue;
      let anc = el.parentNode;
      let idx = tokens.length - 2;
      while (anc && idx >= 0) {
        if (matchesSingleCompound(anc, tokens[idx])) {
          idx -= 1;
        }
        anc = anc.parentNode;
      }
      if (idx < 0) return true;
    }
  }
  return false;
}

function createDomShim() {
  const docListeners = new Map();
  const winListeners = new Map();
  let activeElement = null;

  class ShimEvent {
    constructor(type, opts = {}) {
      this.type = type;
      this.bubbles = opts.bubbles ?? true;
      this.cancelable = opts.cancelable ?? true;
      this.defaultPrevented = false;
      this.target = null;
      this.currentTarget = null;
      this._stopped = false;
      Object.assign(this, opts);
    }
    preventDefault() {
      this.defaultPrevented = true;
    }
    stopPropagation() {
      this._stopped = true;
    }
  }

  class ShimClassList {
    constructor(el) {
      this.el = el;
    }
    _set() {
      const raw = (this.el._attrs.get("class") ?? "").trim();
      return new Set(raw ? raw.split(/\s+/) : []);
    }
    _write(set) {
      this.el._attrs.set("class", [...set].join(" "));
    }
    add(...names) {
      const s = this._set();
      names.forEach((n) => n && s.add(n));
      this._write(s);
    }
    remove(...names) {
      const s = this._set();
      names.forEach((n) => s.delete(n));
      this._write(s);
    }
    contains(name) {
      return this._set().has(name);
    }
    toggle(name, force) {
      const s = this._set();
      const next = force === undefined ? !s.has(name) : Boolean(force);
      if (next) s.add(name);
      else s.delete(name);
      this._write(s);
      return next;
    }
  }

  class ShimElement {
    constructor(tagName) {
      this.nodeType = 1;
      this.tagName = tagName.toUpperCase();
      this.children = [];
      this.parentNode = null;
      this._attrs = new Map();
      this._listeners = new Map();
      this._text = "";
      this._value = "";
      this.disabled = false;
      this.hidden = false;
      this.style = {};
      this.classList = new ShimClassList(this);
      this.dataset = new Proxy(
        {},
        {
          get: (_t, prop) => {
            const attr =
              "data-" +
              String(prop).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
            return this._attrs.get(attr);
          },
          set: (_t, prop, val) => {
            const attr =
              "data-" +
              String(prop).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
            this._attrs.set(attr, String(val));
            return true;
          },
        }
      );
    }

    get isConnected() {
      let cur = this;
      while (cur.parentNode) cur = cur.parentNode;
      return cur === doc.body || cur === doc.documentElement;
    }

    get id() {
      return this._attrs.get("id") ?? "";
    }
    set id(v) {
      this._attrs.set("id", String(v));
    }

    get className() {
      return this._attrs.get("class") ?? "";
    }
    set className(v) {
      this._attrs.set("class", String(v));
    }

    get value() {
      return this._value;
    }
    set value(v) {
      this._value = String(v ?? "");
    }

    get href() {
      return this._attrs.get("href") ?? "";
    }
    set href(v) {
      this._attrs.set("href", String(v));
    }

    get title() {
      return this._attrs.get("title") ?? "";
    }
    set title(v) {
      this._attrs.set("title", String(v));
    }

    setAttribute(name, val) {
      const k = name.toLowerCase();
      if (k === "disabled") this.disabled = true;
      if (k === "hidden") this.hidden = true;
      if (k === "value") this._value = String(val);
      this._attrs.set(k, String(val));
    }
    getAttribute(name) {
      const k = name.toLowerCase();
      if (k === "disabled") return this.disabled ? "" : null;
      if (k === "hidden") return this.hidden ? "" : null;
      return this._attrs.get(k) ?? null;
    }
    hasAttribute(name) {
      return this.getAttribute(name) !== null;
    }
    removeAttribute(name) {
      const k = name.toLowerCase();
      if (k === "disabled") this.disabled = false;
      if (k === "hidden") this.hidden = false;
      this._attrs.delete(k);
    }

    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = this;
      this.children.push(child);
      return child;
    }
    prepend(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = this;
      this.children.unshift(child);
    }
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx >= 0) {
        this.children.splice(idx, 1);
        child.parentNode = null;
      }
      return child;
    }
    remove() {
      this.parentNode?.removeChild(this);
    }

    contains(node) {
      let cur = node;
      while (cur) {
        if (cur === this) return true;
        cur = cur.parentNode;
      }
      return false;
    }

    closest(selector) {
      let cur = this;
      while (cur) {
        if (matchesSelector(cur, selector)) return cur;
        cur = cur.parentNode;
      }
      return null;
    }

    querySelectorAll(selector) {
      const out = [];
      const walk = (node) => {
        for (const ch of node.children) {
          if (matchesSelector(ch, selector)) out.push(ch);
          walk(ch);
        }
      };
      walk(this);
      return out;
    }

    querySelector(selector) {
      return this.querySelectorAll(selector)[0] ?? null;
    }

    get textContent() {
      if (this.children.length === 0) return this._text;
      return this._text + this.children.map((c) => c.textContent).join("");
    }
    set textContent(v) {
      this.children = [];
      this._text = String(v ?? "");
    }

    get innerHTML() {
      return this.textContent;
    }
    set innerHTML(html) {
      this.children = [];
      this._text = "";
      parseHtmlInto(this, String(html ?? ""));
    }
    insertAdjacentHTML(position, html) {
      const tmp = new ShimElement("div");
      parseHtmlInto(tmp, String(html ?? ""));
      if (position === "afterbegin") {
        for (let i = tmp.children.length - 1; i >= 0; i--) {
          this.prepend(tmp.children[i]);
        }
      } else if (position === "beforeend") {
        for (const ch of [...tmp.children]) {
          this.appendChild(ch);
        }
      }
    }

    addEventListener(type, fn) {
      if (!this._listeners.has(type)) this._listeners.set(type, []);
      this._listeners.get(type).push(fn);
    }
    removeEventListener(type, fn) {
      const list = this._listeners.get(type);
      if (!list) return;
      const idx = list.indexOf(fn);
      if (idx >= 0) list.splice(idx, 1);
    }
    dispatchEvent(evt) {
      evt.target = evt.target ?? this;
      let cur = this;
      while (cur && !evt._stopped) {
        evt.currentTarget = cur;
        for (const fn of cur._listeners.get(evt.type) ?? []) {
          fn(evt);
        }
        if (!evt.bubbles) break;
        cur = cur.parentNode;
      }
      return !evt.defaultPrevented;
    }

    focus() {
      activeElement = this;
    }
    click() {
      this.dispatchEvent(new ShimEvent("click"));
    }
  }

  const VOID_TAGS = new Set(["input", "br", "hr", "img", "meta", "link"]);
  function parseHtmlInto(root, html) {
    const stack = [root];
    const tokenRe = /<(\/?)([a-zA-Z0-9-]+)([^>]*)>|([^<]+)/g;
    let match;
    while ((match = tokenRe.exec(html)) !== null) {
      const [, isClose, tagRaw, attrStr, textRaw] = match;
      const top = stack[stack.length - 1];
      if (textRaw !== undefined) {
        const decoded = decodeEntities(textRaw);
        if (top.children.length === 0) top._text += decoded;
        else {
          const span = new ShimElement("#TEXT");
          span._text = decoded;
          top.appendChild(span);
        }
        continue;
      }
      const tag = tagRaw.toLowerCase();
      if (isClose) {
        for (let i = stack.length - 1; i >= 1; i--) {
          if (stack[i].tagName.toLowerCase() === tag) {
            stack.length = i;
            break;
          }
        }
        continue;
      }
      const el = new ShimElement(tag);
      const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let aMatch;
      const cleanedAttr = attrStr.replace(/\/$/, "");
      while ((aMatch = attrRe.exec(cleanedAttr)) !== null) {
        const [, k, v1, v2, v3] = aMatch;
        const val = v1 ?? v2 ?? v3 ?? "";
        el.setAttribute(k, decodeEntities(val));
      }
      top.appendChild(el);
      if (!VOID_TAGS.has(tag) && !attrStr.trim().endsWith("/")) {
        stack.push(el);
      }
    }
  }

  const doc = {
    body: new ShimElement("body"),
    documentElement: new ShimElement("html"),
    createElement: (tag) => new ShimElement(tag),
    get activeElement() {
      return activeElement;
    },
    addEventListener(type, fn, capture = false) {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push({ fn, capture: Boolean(capture) });
    },
    removeEventListener(type, fn, capture = false) {
      const list = docListeners.get(type);
      if (!list) return;
      const idx = list.findIndex(
        (entry) => entry.fn === fn && entry.capture === Boolean(capture)
      );
      if (idx >= 0) list.splice(idx, 1);
    },
    dispatchKeyDown(opts) {
      const evt = new ShimEvent("keydown", opts);
      evt.target = activeElement ?? doc.body;
      for (const entry of [...(docListeners.get("keydown") ?? [])]) {
        if (entry.capture) entry.fn(evt);
      }
      if (!evt._stopped) {
        (evt.target ?? doc.body).dispatchEvent(evt);
      }
      for (const entry of [...(docListeners.get("keydown") ?? [])]) {
        if (!entry.capture && !evt._stopped) entry.fn(evt);
      }
      return evt;
    },
  };
  doc.documentElement.appendChild(doc.body);

  const win = {
    location: { origin: "http://localhost:8080", pathname: "/", search: "" },
    requestAnimationFrame: (cb) => {
      cb(0);
      return 1;
    },
    getComputedStyle: (el) => ({
      display: el.hidden || el.closest(".hidden") ? "none" : "block",
      visibility: "visible",
    }),
    addEventListener: (type, fn) => {
      if (!winListeners.has(type)) winListeners.set(type, []);
      winListeners.get(type).push(fn);
    },
    removeEventListener: (type, fn) => {
      const list = winListeners.get(type);
      if (!list) return;
      const idx = list.indexOf(fn);
      if (idx >= 0) list.splice(idx, 1);
    },
  };

  return { doc, win, ShimElement, ShimEvent };
}

async function bundleTsModule(relPath) {
  const res = await esbuild.build({
    entryPoints: [path.join(WEB_DIR, relPath)],
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
  });
  const code = res.outputFiles[0].text;
  const b64 = Buffer.from(code, "utf8").toString("base64");
  return await import(`data:text/javascript;base64,${b64}`);
}

function withDom(fn) {
  const origWindow = globalThis.window;
  const origDocument = globalThis.document;
  const origHTMLElement = globalThis.HTMLElement;
  const origFetch = globalThis.fetch;

  const { doc, win, ShimElement, ShimEvent } = createDomShim();
  globalThis.window = win;
  globalThis.document = doc;
  globalThis.HTMLElement = ShimElement;

  return Promise.resolve()
    .then(() => fn({ doc, win, ShimEvent }))
    .finally(() => {
      globalThis.window = origWindow;
      globalThis.document = origDocument;
      globalThis.HTMLElement = origHTMLElement;
      globalThis.fetch = origFetch;
    });
}

function makeSession(overrides = {}) {
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
      turnstile_required: false,
      turnstile_sitekey: null,
      google_oauth_enabled: false,
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
      global_daily_public_used: 0,
    },
    ...overrides,
  };
}

test("TrapFocusHelper cycles Tab and Shift+Tab, skips hidden/disabled elements, and restores previous focus", async () => {
  const { TrapFocusHelper } = await bundleTsModule("src/modalFocus.ts");
  await withDom(({ doc }) => {
    const trigger = doc.createElement("button");
    doc.body.appendChild(trigger);
    trigger.focus();

    const panel = doc.createElement("div");
    panel.innerHTML = `
      <button id="b1">First</button>
      <button id="b-hidden" hidden>Hidden</button>
      <button id="b-disabled" disabled>Disabled</button>
      <input id="in1" type="text" />
      <button id="b2">Last</button>
    `;
    doc.body.appendChild(panel);

    const b1 = panel.querySelector("#b1");
    const b2 = panel.querySelector("#b2");
    const trap = new TrapFocusHelper(panel);
    trap.activate(b1);
    assert.equal(doc.activeElement, b1);

    // Shift+Tab from first wraps to last (skipping hidden/disabled)
    const eShift = doc.dispatchKeyDown({ key: "Tab", shiftKey: true });
    assert.equal(eShift.defaultPrevented, true);
    assert.equal(doc.activeElement, b2);

    // Tab from last wraps back to first
    const eTab = doc.dispatchKeyDown({ key: "Tab", shiftKey: false });
    assert.equal(eTab.defaultPrevented, true);
    assert.equal(doc.activeElement, b1);

    // Deactivate restores focus to original trigger button
    trap.deactivate();
    assert.equal(doc.activeElement, trigger);
  });
});

test("AuthModal.setSession preserves open signin password input and focus when auth view state is unchanged (G6) and escapes XSS", async () => {
  const { AuthModal } = await bundleTsModule("src/components/authModal.ts");
  await withDom(({ doc }) => {
    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const modal = new AuthModal(container, {
      onSessionChanged: () => {},
      onOpenAdmin: () => {},
      onToast: () => {},
    });

    const guestSess = makeSession();
    modal.setSession(guestSess);
    modal.open({ mode: "signin" });

    const pwInput = container.querySelector("#gf-auth-password");
    assert.ok(pwInput);
    pwInput.value = "MySecretPass123!";
    pwInput.focus();
    assert.equal(doc.activeElement, pwInput);

    // Background session refresh with unchanged auth state must NOT wipe password or focus
    modal.setSession(
      makeSession({
        quotas: { ...guestSess.quotas, daily_renders_used: 1, daily_renders_remaining: 1 },
      })
    );
    const pwAfter = container.querySelector("#gf-auth-password");
    assert.equal(pwAfter, pwInput);
    assert.equal(pwAfter.value, "MySecretPass123!");
    assert.equal(doc.activeElement, pwInput);

    // Transitioning to authenticated account view DOES re-render and escapes XSS in display_name/email
    modal.setSession(
      makeSession({
        authenticated: true,
        role: "friend",
        user: {
          user_id: "usr_xss",
          email: 'evil+<script>alert(1)</script>@example.com',
          display_name: '<img src=x onerror=alert(1)>Scout',
          role: "friend",
        },
      })
    );
    assert.equal(container.querySelectorAll("script").length, 0);
    assert.equal(container.querySelectorAll("img").length, 0);
    assert.ok(container.textContent.includes("<img src=x onerror=alert(1)>Scout"));
  });
});

test("AdminModal renders partial usage data with error banner and escapes XSS payloads", async () => {
  const { AdminModal } = await bundleTsModule("src/components/adminModal.ts");
  await withDom(async ({ doc }) => {
    globalThis.fetch = async (url) => {
      if (String(url).includes("/api/admin/usage")) {
        return new Response(
          JSON.stringify({
            summary: {
              renders_today: 7,
              renders_7d: 21,
              previews_today: 4,
              active_queue_depth: 1,
              global_daily_public_used: 3,
              global_daily_public_limit: 50,
              public_renders_paused: false,
              avg_elapsed_seconds: 12.4,
              max_peak_rss_mb: 640,
            },
            by_role: {},
            recent_events: [
              {
                event_id: 1,
                event_type: "job_render",
                actor_id: '<img src=x onerror=alert(2)>',
                actor_role: "friend",
                client_ip: "198.51.100.1",
                job_id: "job12345678",
                detail: { note: "<script>xss()</script>" },
                created_at: "2026-11-26T00:00:00Z",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      return new Response(JSON.stringify({ detail: "Users endpoint unavailable" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    };

    const toasts = [];
    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const adminModal = new AdminModal(container, {
      getCurrentUserId: () => "usr_admin",
      onSettingsChanged: () => {},
      onCopyText: () => {},
      onToast: (msg, kind) => toasts.push({ msg, kind }),
    });

    adminModal.open("usage");
    await adminModal.refreshAll();
    await new Promise((r) => setTimeout(r, 10));

    assert.ok(toasts.some((t) => t.kind === "error" && t.msg.includes("Users endpoint unavailable")));
    assert.ok(container.textContent.includes("7"));
    assert.ok(container.textContent.includes("21"));
    assert.equal(container.querySelectorAll("script").length, 0);
    assert.equal(container.querySelectorAll("img").length, 0);
  });
});

test("TelemetryBar escapes XSS in hosted account pill and styles.css enforces [hidden] display:none (G10)", async () => {
  const { TelemetryBar } = await bundleTsModule("src/components/telemetry.ts");
  await withDom(({ doc }) => {
    const container = doc.createElement("header");
    doc.body.appendChild(container);
    const bar = new TelemetryBar(container, {
      onToggleDrawer: () => {},
      onToggleProjects: () => {},
      onCopyMgrs: () => {},
    });

    bar.updateSession(
      makeSession({
        authenticated: true,
        role: "friend",
        user: {
          user_id: "usr_1",
          email: "f@example.com",
          display_name: '<img src=x onerror=alert(9)>Ranger',
          role: "friend",
        },
      })
    );

    assert.equal(container.querySelectorAll("img").length, 0);
    assert.ok(container.textContent.includes("Friend"));
  });

  const css = fs.readFileSync(path.join(WEB_DIR, "src/styles.css"), "utf8");
  assert.match(css, /\[hidden\]\s*\{[^}]*display:\s*none\s*!important;/);
});

test("TelemetryBar removes duplicate Admin/Queue pills, exposes keyboard account menu, and toggles sidebar", async () => {
  const { TelemetryBar } = await bundleTsModule("src/components/telemetry.ts");
  await withDom(({ doc }) => {
    const events = [];
    const container = doc.createElement("header");
    doc.body.appendChild(container);
    const bar = new TelemetryBar(container, {
      onToggleSidebar: () => events.push("toggle-sidebar"),
      onToggleDrawer: () => events.push("toggle-drawer"),
      onToggleProjects: () => events.push("toggle-projects"),
      onCopyMgrs: (m) => events.push(`copy:${m}`),
      onOpenAuth: () => events.push("open-auth"),
      onSignOut: () => events.push("sign-out"),
      onOpenAdmin: () => events.push("open-admin"),
    });

    // Semantic button for MGRS copy and top-left sidebar toggle
    const mgrsBtn = container.querySelector("[data-role='mgrs-box']");
    assert.equal(mgrsBtn.tagName, "BUTTON");

    const sidebarBtn = container.querySelector("[data-role='sidebar-toggle']");
    assert.equal(sidebarBtn.getAttribute("aria-expanded"), "true");
    sidebarBtn.click();
    assert.deepEqual(events, ["toggle-sidebar"]);
    bar.updateSidebarCollapsed(true);
    assert.equal(sidebarBtn.getAttribute("aria-expanded"), "false");

    // Unified Queue button integrates worker health status (no separate status-pill)
    assert.equal(container.querySelector("[data-role='status-pill']"), null);
    const queueBtn = container.querySelector("[data-role='queue-toggle']");
    bar.updateHealth({
      status: "ok",
      queue_depth: 0,
      worker_alive: true,
      worker_last_seen: "2026-11-26T11:00:00Z",
      memory_budget_mb: 3072,
      packs_count: 0,
    });
    assert.equal(queueBtn.classList.contains("is-offline"), false);
    bar.updateHealth(null, "down");
    assert.equal(queueBtn.classList.contains("is-offline"), true);
    assert.ok(queueBtn.textContent.includes("Offline"));

    // Admin session: only ONE Admin shortcut button + monogram account menu (no duplicate Admin button)
    bar.updateSession(
      makeSession({
        authenticated: true,
        role: "admin",
        user: {
          user_id: "usr_admin",
          email: "ops@example.com",
          display_name: "Admin",
          role: "admin",
        },
      })
    );

    const adminShortcut = container.querySelector("[data-role='admin-open-btn']");
    const accountTrigger = container.querySelector("[data-role='auth-account-btn']");
    const accountMenu = container.querySelector("[data-role='auth-account-menu']");
    assert.ok(adminShortcut);
    assert.ok(accountTrigger);
    assert.ok(accountMenu);
    assert.equal(accountTrigger.textContent.includes("Admin"), false);
    assert.ok(accountTrigger.textContent.includes("OP"));
    assert.equal(accountMenu.hidden, true);
    assert.equal(accountTrigger.getAttribute("aria-expanded"), "false");

    // Clicking account trigger opens menu; Escape closes & returns focus
    accountTrigger.click();
    assert.equal(accountMenu.hidden, false);
    assert.equal(accountTrigger.getAttribute("aria-expanded"), "true");

    doc.dispatchKeyDown({ key: "Escape" });
    assert.equal(accountMenu.hidden, true);
    assert.equal(accountTrigger.getAttribute("aria-expanded"), "false");
    assert.equal(doc.activeElement, accountTrigger);

    // Reopen and click Sign Out menu item
    accountTrigger.click();
    const signOutItem = container.querySelector("[data-role='auth-signout-btn']");
    assert.ok(signOutItem);
    signOutItem.click();
    assert.equal(accountMenu.hidden, true);
    assert.ok(events.includes("sign-out"));

    // Direct Admin button opens admin dashboard
    adminShortcut.click();
    assert.ok(events.includes("open-admin"));

    // Custom live deployment branding (e.g. GridMaps) updates header & document title
    bar.updateSession(
      makeSession({
        brand_name: "GridMaps",
        brand_subtitle: "Studio",
        brand_tagline: "Tactical Topographic Print Studio",
      })
    );
    assert.equal(container.querySelector("[data-role='brand-name']").textContent, "GridMaps");
    assert.equal(container.querySelector("[data-role='brand-sub']").textContent, "Studio");
    assert.match(container.querySelector("[data-role='brand']").title, /Powered by open-source GridForge/);
    assert.equal(doc.title, "GridMaps — Tactical Topographic Print Studio");
  });
});
