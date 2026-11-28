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
    replaceChildren(...nodes) {
      for (const ch of [...this.children]) {
        ch.parentNode = null;
      }
      this.children = [];
      this._text = "";
      for (const n of nodes) {
        if (typeof n === "string") this._text += n;
        else if (n) this.appendChild(n);
      }
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

    addEventListener(type, fn, options) {
      const signal = options && typeof options === "object" ? options.signal : undefined;
      if (signal?.aborted) return;
      if (!this._listeners.has(type)) this._listeners.set(type, []);
      this._listeners.get(type).push(fn);
      if (signal) {
        signal.addEventListener("abort", () => this.removeEventListener(type, fn), { once: true });
      }
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
    loader: { ".css": "empty" },
    plugins: [
      {
        name: "vite-url-asset",
        setup(build) {
          build.onResolve({ filter: /\?url$/ }, (args) => ({
            path: args.path,
            namespace: "vite-url",
          }));
          build.onLoad({ filter: /.*/, namespace: "vite-url" }, (args) => ({
            contents: `export default ${JSON.stringify(
              "/assets/" + path.basename(args.path.replace(/\?url$/, ""))
            )};`,
            loader: "js",
          }));
        },
      },
    ],
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
      changeSession: (request) => request(),
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

test("TelemetryBar Share popover toggles cleanly, invokes direct/short share callbacks, and escapes text", async () => {
  const { TelemetryBar } = await bundleTsModule("src/components/telemetry.ts");
  await withDom(({ doc }) => {
    const events = [];
    const container = doc.createElement("header");
    doc.body.appendChild(container);
    const bar = new TelemetryBar(container, {
      onToggleDrawer: () => {},
      onToggleProjects: () => {},
      onCopyMgrs: () => {},
      onShareOpen: () => {
        events.push("share-open");
        bar.setShareSummary({
          headline: '<script>alert(1)</script>Kosciuszko',
          detail: "1:25,000 · A4 · alpine",
          directTruncated: true,
          hasLocalOverlayFile: true,
        });
      },
      onCopyDirectLink: () => events.push("copy-direct"),
      onCreateShortLink: () => {
        events.push("create-short");
        bar.setShortLinkResult({
          url: "https://gridforge.example/?share=AbCdEf1234567890",
          expiresLabel: "Expires in 7d",
        });
      },
      onCopyShareUrl: (u) => events.push(`copy-short:${u}`),
    });

    const toggle = container.querySelector("[data-role='share-toggle']");
    const popover = container.querySelector("[data-role='share-popover']");
    const directBtn = container.querySelector("[data-role='share-copy-direct']");
    const shortBtn = container.querySelector("[data-role='share-create-short']");
    const shortBox = container.querySelector("[data-role='share-short-result']");
    const shortCopyBtn = container.querySelector("[data-role='share-short-copy']");

    assert.equal(popover.hidden, true);
    assert.equal(shortBtn.hidden, false);

    toggle.click();
    assert.equal(popover.hidden, false);
    assert.equal(toggle.getAttribute("aria-expanded"), "true");
    assert.equal(container.querySelectorAll("script").length, 0);
    assert.equal(
      container.querySelector("[data-role='share-note']").hidden,
      false
    );

    directBtn.click();
    shortBtn.click();
    assert.equal(shortBox.hidden, false);
    shortCopyBtn.click();

    assert.deepEqual(events, [
      "share-open",
      "copy-direct",
      "create-short",
      "copy-short:https://gridforge.example/?share=AbCdEf1234567890",
    ]);

    doc.dispatchKeyDown({ key: "Escape" });
    assert.equal(popover.hidden, true);
    assert.equal(toggle.getAttribute("aria-expanded"), "false");
    assert.equal(doc.activeElement, toggle);
  });
});

test("ProjectsDrawer renders Share action per saved project and triggers callback", async () => {
  const { ProjectsDrawer } = await bundleTsModule("src/components/projectsDrawer.ts");
  const origFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        projects: [
          {
            project_id: "proj_alpha1234",
            name: "Alpine Traverse",
            notes: "Winter route",
            created_at: "2026-11-27T00:00:00Z",
            updated_at: "2026-11-27T00:00:00Z",
            state: { center: "-36.45, 148.26", scale: 25000, paper: "a4", color_mode: "alpine" },
          },
        ],
      }),
      { status: 200, headers: { "content-type": "application/json" } }
    );

  try {
    await withDom(async ({ doc }) => {
      const sharedIds = [];
      const container = doc.createElement("aside");
      doc.body.appendChild(container);
      const drawer = new ProjectsDrawer(container, {
        onClose: () => {},
        onLoadProject: () => {},
        onShareProject: async (proj) => {
          sharedIds.push(proj.project_id);
        },
        getCurrentSnapshot: () => ({
          suggestedName: "Test",
          state: {},
          last_job_id: null,
        }),
        onToast: () => {},
      });

      await drawer.refresh();
      const shareBtn = container.querySelector("button[data-proj-action='share']");
      assert.ok(shareBtn);
      shareBtn.click();
      await new Promise((r) => setTimeout(r, 10));
      assert.deepEqual(sharedIds, ["proj_alpha1234"]);
    });
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("ProjectsDrawer resets draft fields and ignores a closed workspace's pending list", async () => {
  const { ProjectsDrawer } = await bundleTsModule("src/components/projectsDrawer.ts");
  await withDom(async ({ doc }) => {
    const container = doc.createElement("aside");
    doc.body.appendChild(container);
    const drawer = new ProjectsDrawer(container, {
      onClose: () => {},
      onLoadProject: () => {},
      getCurrentSnapshot: () => ({ suggestedName: "Training draft", state: {}, last_job_id: null }),
      onToast: () => {},
    });
    const name = container.querySelector("[data-role='proj-name']");
    const notes = container.querySelector("[data-role='proj-notes']");
    drawer.prepareSuggestedName();
    name.value = "Field exercise";
    notes.value = "Draft itinerary";

    let finishRequest;
    let requestSignal;
    const origFetch = globalThis.fetch;
    globalThis.fetch = (_url, init) => {
      requestSignal = init.signal;
      return new Promise((resolve) => { finishRequest = resolve; });
    };
    try {
      const pending = drawer.refresh();
      drawer.clear();
      finishRequest(new Response(JSON.stringify({ projects: [{
        project_id: "proj_closed_workspace",
        name: "Training itinerary",
        created_at: "2026-11-27T00:00:00Z",
        updated_at: "2026-11-27T00:00:00Z",
        state: {},
      }] })));
      await pending;
    } finally {
      globalThis.fetch = origFetch;
    }

    assert.equal(requestSignal.aborted, true);
    assert.equal(name.value, "");
    assert.equal(notes.value, "");
    assert.equal(name.placeholder, "Project name (e.g. Kosciuszko Winter Loop)");
    assert.match(container.querySelector("[data-role='projects-list']").textContent, /No saved trip projects/);
  });
});

test("AdminModal.dispose aborts in-flight requests and removes root click listeners without an external signal", async () => {
  const { AdminModal } = await bundleTsModule("src/components/adminModal.ts");
  await withDom(async ({ doc }) => {
    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const modal = new AdminModal(container, {
      getCurrentUserId: () => "usr_admin",
      onSettingsChanged: () => {},
      onToast: () => {},
      onCopyText: () => {},
    });

    const signals = [];
    const origFetch = globalThis.fetch;
    globalThis.fetch = (_url, init) => {
      signals.push(init.signal);
      return new Promise(() => {});
    };
    try {
      void modal.refreshAll();
      assert.equal(signals.length, 2);
      assert.equal(signals.every((s) => !s.aborted), true);
      modal.dispose();
      assert.equal(signals.every((s) => s.aborted), true);
      assert.equal(container.children.length, 0);
      assert.equal((container._listeners.get("click") ?? []).length, 0);
    } finally {
      globalThis.fetch = origFetch;
    }
  });
});

test("AuthModal.clearAccountState closes dialog and clears sensitive draft inputs", async () => {
  const { AuthModal } = await bundleTsModule("src/components/authModal.ts");
  await withDom(async ({ doc }) => {
    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const modal = new AuthModal(container, {
      changeSession: (request) => request(),
      onOpenAdmin: () => {},
      onToast: () => {},
    });
    modal.open({ mode: "invite", inviteToken: "secret-invite-tok", bannerError: "Prior error" });
    assert.equal(modal.opened, true);
    modal.clearAccountState();
    assert.equal(modal.opened, false);
    assert.equal(container.querySelector("[data-role='auth-body']").children.length, 0);
    modal.open({ mode: "invite" });
    assert.equal(container.querySelector("#gf-invite-token").value, "");
  });
});

test("AuthModal invite submit normalizes blank optional fields to undefined and preserves values", async () => {
  const { AuthModal } = await bundleTsModule("src/components/authModal.ts");
  await withDom(async ({ doc, ShimEvent }) => {
    const sentPayloads = [];
    globalThis.fetch = async (url, init) => {
      if (String(url) === "/api/auth/accept-invite") {
        sentPayloads.push(JSON.parse(String(init.body)));
        return new Response(JSON.stringify(makeSession({ authenticated: true, role: "friend" })), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    };

    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const modal = new AuthModal(container, {
      changeSession: (request) => request(),
      onOpenAdmin: () => {},
      onToast: () => {},
    });

    // 1. Blank email & display name must become undefined (omitted from JSON)
    modal.open({ mode: "invite", inviteToken: "tok-blank-opt" });
    container.querySelector("#gf-invite-password").value = "LongPassword123!";
    container.querySelector("[data-role='invite-form']").dispatchEvent(new ShimEvent("submit"));
    await new Promise((r) => setTimeout(r, 15));

    assert.equal(sentPayloads.length, 1);
    assert.equal(sentPayloads[0].token, "tok-blank-opt");
    assert.equal("email" in sentPayloads[0], false);
    assert.equal("display_name" in sentPayloads[0], false);

    // 2. Non-empty email & display name are preserved verbatim
    modal.open({ mode: "invite", inviteToken: "tok-full-opt" });
    container.querySelector("#gf-invite-email").value = "ranger@example.com";
    container.querySelector("#gf-invite-name").value = "Ranger";
    container.querySelector("#gf-invite-password").value = "LongPassword123!";
    container.querySelector("[data-role='invite-form']").dispatchEvent(new ShimEvent("submit"));
    await new Promise((r) => setTimeout(r, 15));

    assert.equal(sentPayloads.length, 2);
    assert.equal(sentPayloads[1].email, "ranger@example.com");
    assert.equal(sentPayloads[1].display_name, "Ranger");
  });
});

test("PacksModal respects pre-aborted and live signals, fetches regions on initial open, caches them, and reports action errors", async () => {
  const { PacksModal } = await bundleTsModule("src/components/packsModal.ts");
  await withDom(async ({ doc }) => {
    const calls = [];
    const toasts = [];
    const apiErrors = [];
    const seenSignals = [];

    // 1. Pre-aborted externalSignal must prevent any network calls on open
    const preAborted = new AbortController();
    preAborted.abort();
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      return new Response(JSON.stringify({}), { status: 200 });
    };
    const deadContainer = doc.createElement("div");
    doc.body.appendChild(deadContainer);
    const deadModal = new PacksModal(
      deadContainer,
      {
        onPacksUpdated: () => {},
        onSelectPack: () => {},
        onBasemapRefresh: () => {},
        onToast: (msg, kind) => toasts.push({ msg, kind }),
        getCurrentBBoxString: () => null,
        onApiError: (err) => apiErrors.push(err),
      },
      preAborted.signal
    );
    deadModal.open();
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(calls.length, 0);

    // 2. Active externalSignal: first open fetches regions, second refresh reuses cache
    const liveCtrl = new AbortController();
    globalThis.fetch = async (url, init = {}) => {
      const sUrl = String(url);
      calls.push(sUrl);
      if (init.signal) seenSignals.push(init.signal);
      if (sUrl === "/api/packs") {
        return new Response(
          JSON.stringify({
            packs: [
              {
                pack_id: "au-blue-mountains",
                name: "Blue Mountains",
                region: "nsw",
                utm_zones: ["56S"],
                vertical_datum: "AHD",
                size_bytes: 1024,
                created_at: "2026-11-27T00:00:00Z",
                has_osm: true,
                has_dem: true,
                has_overlays: false,
                has_basemap: false,
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      if (sUrl === "/api/packs/regions") {
        return new Response(
          JSON.stringify({
            regions: [
              {
                region_id: "blue-mountains",
                name: "Blue Mountains",
                group: "au_tactical",
                utm_zones: ["56S"],
                bbox_wgs84: { min_x: 150.1, min_y: -33.9, max_x: 150.5, max_y: -33.5 },
                estimated_size_hint: "~120 MB",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      if (sUrl === "/api/packs/tasks") {
        return new Response(JSON.stringify({ tasks: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (sUrl === "/api/packs/build" || sUrl.startsWith("/api/packs/au-blue-mountains")) {
        return new Response(JSON.stringify({ detail: "Pack operation denied" }), {
          status: 403,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    };

    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const modal = new PacksModal(
      container,
      {
        onPacksUpdated: () => {},
        onSelectPack: () => {},
        onBasemapRefresh: () => {},
        onToast: (msg, kind) => toasts.push({ msg, kind }),
        getCurrentBBoxString: () => "150.1,-33.9,150.5,-33.5",
        onApiError: (err) => apiErrors.push(err),
      },
      liveCtrl.signal
    );

    modal.open();
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(calls.filter((u) => u === "/api/packs/regions").length, 1);

    // Refresh again: regions must now come from cache without re-fetching
    container.querySelector("[data-role='modal-refresh']").click();
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(calls.filter((u) => u === "/api/packs/regions").length, 1);

    // Build and delete failures report toast + onApiError
    const buildBtn = container.querySelector("button[data-region-action='build']");
    assert.ok(buildBtn);
    buildBtn.click();
    await new Promise((r) => setTimeout(r, 15));

    const delBtn = container.querySelector("button[data-pack-action='delete']");
    assert.ok(delBtn);
    delBtn.click();
    await new Promise((r) => setTimeout(r, 15));

    assert.equal(toasts.filter((t) => t.kind === "error" && t.msg.includes("Pack operation denied")).length, 2);
    assert.equal(apiErrors.length, 2);

    // Aborting external signal cascades to linked lifecycle signal
    liveCtrl.abort();
    assert.equal(seenSignals.every((s) => s.aborted), true);
    modal.dispose();
    assert.equal(container.children.length, 0);
  });
});

test("AdminModal respects pre-aborted externalSignal and cascades external abort", async () => {
  const { AdminModal } = await bundleTsModule("src/components/adminModal.ts");
  await withDom(async ({ doc }) => {
    const calls = [];
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      return new Response(JSON.stringify({}), { status: 200 });
    };

    const preAborted = new AbortController();
    preAborted.abort();
    const c1 = doc.createElement("div");
    doc.body.appendChild(c1);
    const deadModal = new AdminModal(
      c1,
      {
        getCurrentUserId: () => "usr_admin",
        onSettingsChanged: () => {},
        onToast: () => {},
        onCopyText: () => {},
      },
      preAborted.signal
    );
    await deadModal.refreshAll();
    assert.equal(calls.length, 0);

    const liveCtrl = new AbortController();
    const liveSignals = [];
    globalThis.fetch = (_url, init) => {
      liveSignals.push(init.signal);
      return new Promise(() => {});
    };
    const c2 = doc.createElement("div");
    doc.body.appendChild(c2);
    const liveModal = new AdminModal(
      c2,
      {
        getCurrentUserId: () => "usr_admin",
        onSettingsChanged: () => {},
        onToast: () => {},
        onCopyText: () => {},
      },
      liveCtrl.signal
    );
    void liveModal.refreshAll();
    assert.equal(liveSignals.length, 2);
    assert.equal(liveSignals.every((s) => !s.aborted), true);
    liveCtrl.abort();
    assert.equal(liveSignals.every((s) => s.aborted), true);
  });
});

test("AdminModal user toggle updates only the target user record and clears busy state", async () => {
  const { AdminModal } = await bundleTsModule("src/components/adminModal.ts");
  await withDom(async ({ doc }) => {
    const users = [
      {
        user_id: "usr_1",
        email: "alpha@example.com",
        display_name: "Alpha",
        role: "friend",
        disabled: false,
        daily_render_limit: null,
        hourly_preview_limit: null,
        created_at: "2026-11-27T00:00:00Z",
        last_login_at: null,
        renders_today: 0,
        previews_today: 0,
      },
      {
        user_id: "usr_2",
        email: "bravo@example.com",
        display_name: "Bravo",
        role: "friend",
        disabled: false,
        daily_render_limit: null,
        hourly_preview_limit: null,
        created_at: "2026-11-27T00:00:00Z",
        last_login_at: null,
        renders_today: 0,
        previews_today: 0,
      },
    ];
    let settingsChanged = 0;
    const toasts = [];
    globalThis.fetch = async (url, init = {}) => {
      const sUrl = String(url);
      if (sUrl === "/api/admin/users" && (!init.method || init.method === "GET")) {
        return new Response(JSON.stringify({ users, invites: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (sUrl === "/api/admin/usage") {
        return new Response(
          JSON.stringify({
            summary: {
              renders_today: 0,
              renders_7d: 0,
              previews_today: 0,
              active_queue_depth: 0,
              global_daily_public_used: 0,
              global_daily_public_limit: 50,
              public_renders_paused: false,
              avg_elapsed_seconds: 0,
              max_peak_rss_mb: 0,
            },
            by_role: {},
            recent_events: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      if (sUrl === "/api/admin/users/usr_1" && init.method === "PATCH") {
        return new Response(
          JSON.stringify({ user: { ...users[0], disabled: true } }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      return new Response(JSON.stringify({}), { status: 200 });
    };

    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const modal = new AdminModal(container, {
      getCurrentUserId: () => "usr_admin",
      onSettingsChanged: () => {
        settingsChanged += 1;
      },
      onToast: (msg, kind) => toasts.push({ msg, kind }),
      onCopyText: () => {},
    });

    modal.open("users");
    await modal.refreshAll();
    const disableBtn = container.querySelector(
      "button[data-admin-action='toggle-user-disabled'][data-user-id='usr_1']"
    );
    assert.ok(disableBtn);
    disableBtn.click();
    await new Promise((r) => setTimeout(r, 20));

    assert.equal(settingsChanged, 1);
    assert.ok(toasts.some((t) => t.msg.includes("alpha@example.com disabled")));
    // Only usr_1 flipped to Enable (next-disabled=false); usr_2 still offers Disable (next-disabled=true)
    const u1Btn = container.querySelector(
      "button[data-admin-action='toggle-user-disabled'][data-user-id='usr_1']"
    );
    const u2Btn = container.querySelector(
      "button[data-admin-action='toggle-user-disabled'][data-user-id='usr_2']"
    );
    assert.equal(u1Btn.disabled, false);
    assert.equal(u1Btn.dataset.nextDisabled, "false");
    assert.equal(u2Btn.dataset.nextDisabled, "true");
  });
});

test("ProjectsDrawer prepareSuggestedName fallback and non-aborted refresh error propagation", async () => {
  const { ProjectsDrawer } = await bundleTsModule("src/components/projectsDrawer.ts");
  await withDom(async ({ doc }) => {
    let suggested = "Ridge Loop";
    const apiErrors = [];
    const container = doc.createElement("aside");
    doc.body.appendChild(container);
    const drawer = new ProjectsDrawer(container, {
      onClose: () => {},
      onLoadProject: () => {},
      getCurrentSnapshot: () => ({ suggestedName: suggested, state: {}, last_job_id: null }),
      onToast: () => {},
      onApiError: (err, ctx) => apiErrors.push({ err, ctx }),
    });

    const nameInput = container.querySelector("[data-role='proj-name']");
    drawer.prepareSuggestedName();
    assert.equal(nameInput.placeholder, "Ridge Loop");

    suggested = "";
    drawer.prepareSuggestedName();
    assert.equal(nameInput.placeholder, "Project name (e.g. Kosciuszko Winter Loop)");

    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: "Session expired" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    await drawer.refresh();
    assert.equal(apiErrors.length, 1);
    assert.equal(apiErrors[0].ctx, "projects");
  });
});

test("TelemetryBar.resetWorkspace closes account menu, clears share truncation state, and resets footprint", async () => {
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
        user: { user_id: "u1", email: "f@example.com", display_name: "Scout", role: "friend" },
      })
    );
    const accountTrigger = container.querySelector("[data-role='auth-account-btn']");
    const accountMenu = container.querySelector("[data-role='auth-account-menu']");
    const shareNote = container.querySelector("[data-role='share-note']");
    const shortBox = container.querySelector("[data-role='share-short-result']");

    accountTrigger.click();
    assert.equal(accountMenu.hidden, false);
    bar.setShareSummary({
      headline: "Alpine",
      detail: "1:25,000",
      directTruncated: true,
      hasLocalOverlayFile: true,
    });
    bar.setShortLinkResult({ url: "https://example.com/?share=abc", expiresLabel: "7d" });
    assert.equal(shareNote.hidden, false);
    assert.equal(shortBox.hidden, false);

    bar.resetWorkspace();
    assert.equal(accountMenu.hidden, true);
    assert.equal(shareNote.hidden, true);
    assert.equal(shortBox.hidden, true);
    assert.equal(container.querySelector("[data-role='mgrs']").textContent, "—");
  });
});

test("SidebarPanel.resetWorkspace and GridForgeMap.resetWorkspace clear workspace controls and offline layers", async () => {
  const { SidebarPanel } = await bundleTsModule("src/components/sidebar.ts");
  const { GridForgeMap } = await bundleTsModule("src/map.ts");

  await withDom(({ doc }) => {
    const tabsEl = doc.createElement("div");
    tabsEl.innerHTML = `
      <button data-preset-tab="nsw-vic">NSW</button>
      <button data-preset-tab="nz" class="active">NZ</button>
    `;
    const dropzoneEl = doc.createElement("div");
    dropzoneEl.classList.add("dragover");
    const rulerCalls = [];
    const sectorCalls = [];

    const fakeSidebar = {
      closeGeocodeMenu: () => {},
      fileInput: { value: "route.gpx" },
      dropzoneEl,
      ringRadiiInput: { value: "5,10", defaultValue: "1,2,5" },
      activePresetTab: "nz",
      activePresetId: "milford",
      presetTabsEl: tabsEl,
      renderPresetCards: () => {},
      setTitlePlaceholder: () => {},
      setRouteMetrics: () => {},
      setRulerState: (...args) => rulerCalls.push(args),
      setSectorDraftCount: (n) => sectorCalls.push(n),
      setPacks: () => {},
      setPlanningStrip: () => {},
      setSubmitting: () => {},
      setFormError: () => {},
      syncControlsFromState: () => {},
    };

    SidebarPanel.prototype.resetWorkspace.call(fakeSidebar);
    const [nswBtn, nzBtn] = tabsEl.querySelectorAll("[data-preset-tab]");
    assert.equal(nswBtn.classList.contains("active"), true);
    assert.equal(nzBtn.classList.contains("active"), false);
    assert.deepEqual(rulerCalls, [[[], null, false]]);
    assert.deepEqual(sectorCalls, [0]);
    assert.equal(fakeSidebar.fileInput.value, "");
    assert.equal(fakeSidebar.ringRadiiInput.value, "1,2,5");

    // GridForgeMap.resetWorkspace removes offline layer only when isLoaded and suppresses near clicks
    let removedCount = 0;
    const makeFakeMap = (isLoaded) => ({
      marker: {
        remove() { return this; },
        setLngLat() { return this; },
        addTo() { return this; },
      },
      map: { jumpTo: () => {} },
      draggingMarker: true,
      suppressClickUntil: 0,
      pendingOfflineBasemap: { id: "p" },
      lastOfflineBasemap: { id: "p" },
      reportedExternalTileFail: true,
      isLoaded,
      removeOfflineBasemapLayer: () => { removedCount += 1; },
      clearFootprint: () => {},
      updateStripRoute: () => {},
      updateWaypoints: () => {},
      updateViewsheds: () => {},
      updateTacticalGraphics: () => {},
      updateRFLinks: () => {},
      updateRuler: () => {},
    });

    const before = performance.now();
    const loadedMap = makeFakeMap(true);
    GridForgeMap.prototype.resetWorkspace.call(loadedMap, -33.7, 150.3);
    assert.equal(removedCount, 1);
    assert.ok(loadedMap.suppressClickUntil > before);

    const unloadedMap = makeFakeMap(false);
    GridForgeMap.prototype.resetWorkspace.call(unloadedMap, -33.7, 150.3);
    assert.equal(removedCount, 1);
  });
});

test("mountMapCanvas releases pointer capture on pointerup only when canvas holds capture", async () => {
  const { mountMapCanvas } = await bundleTsModule("mcp-app/mapCanvas.ts");
  await withDom(({ doc, win, ShimEvent }) => {
    const origGetComputedStyle = globalThis.getComputedStyle;
    win.devicePixelRatio = 1;
    win.getComputedStyle = () => ({
      getPropertyValue: () => "",
    });
    globalThis.getComputedStyle = win.getComputedStyle;
    const container = doc.createElement("div");
    doc.body.appendChild(container);
    const noopCtx = new Proxy(
      {},
      {
        get(_t, prop) {
          if (prop === "measureText") return () => ({ width: 20 });
          return () => {};
        },
      }
    );

    const origCreate = doc.createElement.bind(doc);
    let hasCapture = false;
    const released = [];
    const origSetInner = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(container),
      "innerHTML"
    ).set;

    const patchCanvas = (canvas) => {
      canvas.getContext = () => noopCtx;
      canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 560, height: 280 });
      canvas.setPointerCapture = () => {};
      canvas.hasPointerCapture = () => hasCapture;
      canvas.releasePointerCapture = (id) => released.push(id);
    };

    // Mount once after letting container parse stage + canvas
    const stageProto = Object.getPrototypeOf(container);
    const origQ = stageProto.querySelector;
    stageProto.querySelector = function (sel) {
      const found = origQ.call(this, sel);
      if (found && sel === "[data-map-canvas]") patchCanvas(found);
      if (found && sel === "[data-map-stage]") {
        found.getBoundingClientRect = () => ({ left: 0, top: 0, width: 560, height: 280 });
      }
      return found;
    };

    try {
      const clicks = [];
      const mounted = mountMapCanvas(
        container,
        { center: { lat: -33.7, lon: 150.3 } },
        { onCenterClick: (lat, lon) => clicks.push({ lat, lon }) }
      );
      const canvas = container.querySelector("[data-map-canvas]");

      // 1. Capture NOT held -> releasePointerCapture must not be called
      hasCapture = false;
      canvas.dispatchEvent(new ShimEvent("pointerdown", { button: 0, clientX: 100, clientY: 100, pointerId: 7 }));
      canvas.dispatchEvent(new ShimEvent("pointerup", { clientX: 100, clientY: 100, pointerId: 7 }));
      assert.deepEqual(released, []);
      assert.equal(clicks.length, 1);

      // 2. Capture held -> releasePointerCapture must be called with pointerId
      hasCapture = true;
      canvas.dispatchEvent(new ShimEvent("pointerdown", { button: 0, clientX: 120, clientY: 120, pointerId: 9 }));
      canvas.dispatchEvent(new ShimEvent("pointerup", { clientX: 120, clientY: 120, pointerId: 9 }));
      assert.deepEqual(released, [9]);
      mounted.destroy();
    } finally {
      stageProto.querySelector = origQ;
      globalThis.getComputedStyle = origGetComputedStyle;
      void origCreate;
      void origSetInner;
    }
  });
});

test("TelemetryBar theme toggle updates aria state and invokes callback; PacksModal surfaces region load error with Retry", async () => {
  const { TelemetryBar } = await bundleTsModule("src/components/telemetry.ts");
  const { PacksModal } = await bundleTsModule("src/components/packsModal.ts");

  await withDom(async ({ doc }) => {
    let toggled = 0;
    const header = doc.createElement("header");
    doc.body.appendChild(header);
    const bar = new TelemetryBar(header, {
      onToggleDrawer: () => {},
      onToggleProjects: () => {},
      onCopyMgrs: () => {},
      onToggleTheme: () => {
        toggled += 1;
      },
    });

    const themeBtn = header.querySelector("[data-role='theme-toggle']");
    assert.ok(themeBtn);
    themeBtn.click();
    assert.equal(toggled, 1);
    bar.updateTheme("light");
    assert.equal(themeBtn.getAttribute("aria-pressed"), "true");
    assert.match(themeBtn.getAttribute("aria-label"), /Dark/i);
    bar.updateTheme("dark");
    assert.equal(themeBtn.getAttribute("aria-pressed"), "false");
    assert.match(themeBtn.getAttribute("aria-label"), /Daylight/i);

    // PacksModal offline/error state renders retry action instead of perpetual loading
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: "API offline" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    const modalHost = doc.createElement("div");
    doc.body.appendChild(modalHost);
    const modal = new PacksModal(modalHost, {
      onPacksUpdated: () => {},
      onSelectPack: () => {},
      onBasemapRefresh: () => {},
      onToast: () => {},
      getCurrentBBoxString: () => null,
    });
    modal.open();
    await new Promise((r) => setTimeout(r, 20));
    const retryBtn = modalHost.querySelector("[data-region-action='retry-regions']");
    assert.ok(retryBtn, "Expected Retry button when /api/packs/regions fails");
    assert.match(modalHost.textContent, /Unable to load curated regions/);
    modal.dispose();

    // Sidebar footer exposes secure GitHub repository link
    const sidebarSource = fs.readFileSync(
      path.join(WEB_DIR, "src/components/sidebar.ts"),
      "utf8"
    );
    assert.match(sidebarSource, /href="https:\/\/github\.com\/jremick\/gridforge"/);
    assert.match(sidebarSource, /rel="noopener noreferrer"/);

    // MapLibre v6 worker URL is explicitly wired to the Vite-emitted worker asset
    const mapMod = await bundleTsModule("src/map.ts");
    assert.match(mapMod.MAPLIBRE_WORKER_URL, /\/assets\/maplibre-gl-worker\.mjs$/);
  });
});
