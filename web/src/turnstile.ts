import type { SessionResponse } from "./types.ts";

export const TURNSTILE_SCRIPT_SRC =
  "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__gfTurnstileReady";

export interface TurnstileRenderOptions {
  sitekey: string;
  theme?: "light" | "dark" | "auto";
  size?: "normal" | "compact" | "flexible";
  appearance?: "always" | "execute" | "interaction-only";
  execution?: "render" | "execute";
  callback?: (token: string) => void;
  "expired-callback"?: () => void;
  "error-callback"?: (errorCode?: string) => void;
  "timeout-callback"?: () => void;
}

export interface TurnstileApi {
  render: (
    container: HTMLElement | string,
    options: TurnstileRenderOptions
  ) => string;
  reset: (widgetId?: string) => void;
  remove: (widgetId?: string) => void;
  getResponse: (widgetId?: string) => string | undefined;
  execute?: (
    container?: HTMLElement | string,
    options?: TurnstileRenderOptions
  ) => void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
    __gfTurnstileReady?: () => void;
  }
}

export interface TurnstileControllerOptions {
  container?: HTMLElement | null;
  loadApi?: () => Promise<TurnstileApi>;
  scriptTimeoutMs?: number;
  onInteractiveNeeded?: () => void;
}

let sharedScriptPromise: Promise<TurnstileApi> | null = null;

export function defaultLoadTurnstileApi(
  timeoutMs = 15000
): Promise<TurnstileApi> {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return Promise.reject(
      new Error("Turnstile is only available in a browser environment.")
    );
  }
  if (window.turnstile) {
    return Promise.resolve(window.turnstile);
  }
  if (sharedScriptPromise) {
    return sharedScriptPromise;
  }

  const setTimer =
    typeof window.setTimeout === "function"
      ? window.setTimeout.bind(window)
      : setTimeout;
  const clearTimer =
    typeof window.clearTimeout === "function"
      ? window.clearTimeout.bind(window)
      : clearTimeout;

  sharedScriptPromise = new Promise<TurnstileApi>((resolve, reject) => {
    let settled = false;
    let scriptEl: HTMLScriptElement | null = null;

    const removeFailedScript = () => {
      const target =
        scriptEl ??
        document.querySelector<HTMLScriptElement>(
          'script[data-gf-turnstile="1"]'
        );
      scriptEl = null;
      if (!target) return;
      if (typeof target.remove === "function") {
        target.remove();
      } else {
        target.parentNode?.removeChild(target);
      }
    };

    const timer = setTimer(() => {
      if (settled) return;
      settled = true;
      sharedScriptPromise = null;
      if (window.__gfTurnstileReady === finishOk) {
        window.__gfTurnstileReady = undefined;
      }
      removeFailedScript();
      reject(
        new Error(
          "Security verification script timed out. Check your connection or content blocker and try again."
        )
      );
    }, timeoutMs);

    const finishOk = () => {
      if (settled) return;
      if (!window.turnstile) {
        settled = true;
        clearTimer(timer);
        sharedScriptPromise = null;
        if (window.__gfTurnstileReady === finishOk) {
          window.__gfTurnstileReady = undefined;
        }
        removeFailedScript();
        reject(new Error("Security verification API failed to initialize."));
        return;
      }
      settled = true;
      clearTimer(timer);
      resolve(window.turnstile);
    };

    const finishErr = () => {
      if (settled) return;
      settled = true;
      clearTimer(timer);
      sharedScriptPromise = null;
      if (window.__gfTurnstileReady === finishOk) {
        window.__gfTurnstileReady = undefined;
      }
      removeFailedScript();
      reject(
        new Error(
          "Security verification could not load (blocked or offline). Allow challenges.cloudflare.com and retry."
        )
      );
    };

    window.__gfTurnstileReady = finishOk;

    const existing = document.querySelector<HTMLScriptElement>(
      'script[data-gf-turnstile="1"]'
    );
    if (existing) {
      scriptEl = existing;
      if (window.turnstile) {
        finishOk();
      } else {
        existing.addEventListener("error", finishErr, { once: true });
      }
      return;
    }

    const script = document.createElement("script");
    script.src = TURNSTILE_SCRIPT_SRC;
    script.async = true;
    script.defer = true;
    script.dataset.gfTurnstile = "1";
    scriptEl = script;
    script.addEventListener("error", finishErr, { once: true });
    (document.head ?? document.documentElement).appendChild(script);
  });

  return sharedScriptPromise;
}

export class TurnstileController {
  private container: HTMLElement | null;
  private readonly customLoadApi?: () => Promise<TurnstileApi>;
  private readonly scriptTimeoutMs: number;
  private readonly onInteractiveNeeded?: () => void;

  private api: TurnstileApi | null = null;
  private active = false;
  private sitekey: string | null = null;
  private renderedSitekey: string | null = null;
  private widgetId: string | null = null;
  private cachedToken: string | null = null;
  private tokenConsumed = false;
  private lastError: string | null = null;

  private pendingWaiter: {
    resolve: (token: string | null) => void;
    reject: (err: Error) => void;
    timer: ReturnType<typeof setTimeout> | null;
  } | null = null;
  private activeAcquires = 0;
  private acquireChain: Promise<unknown> = Promise.resolve();

  constructor(options: TurnstileControllerOptions = {}) {
    this.container = options.container ?? null;
    this.customLoadApi = options.loadApi;
    this.scriptTimeoutMs = options.scriptTimeoutMs ?? 15000;
    this.onInteractiveNeeded = options.onInteractiveNeeded;
  }

  setContainer(container: HTMLElement | null): void {
    this.container = container;
    this.setContainerVisible(this.active);
  }

  isActive(): boolean {
    return this.active;
  }

  getWidgetId(): string | null {
    return this.widgetId;
  }

  getLastError(): string | null {
    return this.lastError;
  }

  private setContainerVisible(visible: boolean): void {
    if (!this.container?.classList) return;
    this.container.classList.toggle("hidden", !visible);
  }

  private async loadApi(): Promise<TurnstileApi> {
    if (this.api) return this.api;
    const loaded = this.customLoadApi
      ? await this.customLoadApi()
      : await defaultLoadTurnstileApi(this.scriptTimeoutMs);
    this.api = loaded;
    return loaded;
  }

  async syncSession(session: SessionResponse): Promise<void> {
    const rawKey = session.capabilities.turnstile_sitekey?.trim() ?? "";
    const required =
      session.auth_mode === "hosted" &&
      session.capabilities.turnstile_required &&
      rawKey.length > 0;

    if (!required) {
      this.unmount();
      return;
    }

    this.active = true;
    this.sitekey = rawKey;
    this.lastError = null;
    this.setContainerVisible(true);

    if (this.widgetId !== null && this.renderedSitekey === rawKey) {
      return;
    }

    if (this.widgetId !== null) {
      this.removeWidgetOnly();
    }

    try {
      await this.renderWidgetIfReady(rawKey);
    } catch (err) {
      this.lastError =
        err instanceof Error
          ? err.message
          : "Security verification could not load.";
    }
  }

  private async renderWidgetIfReady(expectedSitekey: string): Promise<void> {
    if (!this.container) return;
    const api = await this.loadApi();
    if (!this.active || this.sitekey !== expectedSitekey || !this.container) {
      return;
    }
    if (this.widgetId !== null && this.renderedSitekey === expectedSitekey) {
      return;
    }

    this.cachedToken = null;
    this.tokenConsumed = false;
    this.lastError = null;
    this.renderedSitekey = expectedSitekey;
    this.widgetId = api.render(this.container, {
      sitekey: expectedSitekey,
      theme: "dark",
      size: "flexible",
      appearance: "interaction-only",
      execution: "render",
      callback: (token: string) => {
        this.lastError = null;
        if (this.pendingWaiter) {
          const waiter = this.pendingWaiter;
          this.pendingWaiter = null;
          if (waiter.timer !== null) clearTimeout(waiter.timer);
          this.cachedToken = null;
          this.tokenConsumed = true;
          waiter.resolve(token);
        } else {
          this.cachedToken = token;
          this.tokenConsumed = false;
        }
      },
      "expired-callback": () => {
        this.cachedToken = null;
        this.tokenConsumed = false;
        if (this.api && this.widgetId !== null) {
          this.api.reset(this.widgetId);
        }
      },
      "error-callback": (errorCode?: string) => {
        this.cachedToken = null;
        this.tokenConsumed = false;
        const msg = errorCode
          ? `Security verification error (${errorCode}). Please try again.`
          : "Security verification failed. Please try again.";
        this.lastError = msg;
        if (this.pendingWaiter) {
          const waiter = this.pendingWaiter;
          this.pendingWaiter = null;
          if (waiter.timer !== null) clearTimeout(waiter.timer);
          waiter.reject(new Error(msg));
        }
      },
      "timeout-callback": () => {
        this.cachedToken = null;
        this.tokenConsumed = false;
        if (this.api && this.widgetId !== null) {
          this.api.reset(this.widgetId);
        }
      },
    });
  }

  acquireToken(timeoutMs = 30000): Promise<string | null> {
    this.activeAcquires += 1;
    const run = this.acquireChain.then(() =>
      this.acquireTokenSerialized(timeoutMs)
    );
    this.acquireChain = run.catch(() => undefined);
    return run;
  }

  private async acquireTokenSerialized(
    timeoutMs: number
  ): Promise<string | null> {
    try {
      if (!this.active || !this.sitekey) {
        return null;
      }

      if (this.widgetId === null) {
        await this.renderWidgetIfReady(this.sitekey);
      }

      if (!this.active) {
        return null;
      }

      if (this.cachedToken && !this.tokenConsumed) {
        const token = this.cachedToken;
        this.cachedToken = null;
        this.tokenConsumed = true;
        return token;
      }

      if (!this.tokenConsumed && this.api && this.widgetId !== null) {
        const existing = this.api.getResponse(this.widgetId)?.trim();
        if (existing) {
          this.cachedToken = null;
          this.tokenConsumed = true;
          return existing;
        }
      }

      if ((this.lastError || this.tokenConsumed) && this.api && this.widgetId !== null) {
        this.lastError = null;
        this.tokenConsumed = false;
        this.api.reset(this.widgetId);

        if (this.cachedToken && !this.tokenConsumed) {
          const token = this.cachedToken;
          this.cachedToken = null;
          this.tokenConsumed = true;
          return token;
        }

        if (!this.tokenConsumed) {
          const recovered = this.api.getResponse?.(this.widgetId)?.trim();
          if (recovered) {
            this.cachedToken = null;
            this.tokenConsumed = true;
            return recovered;
          }
        }

        if (this.lastError) {
          throw new Error(this.lastError);
        }
      }

      this.onInteractiveNeeded?.();

      return await new Promise<string | null>((resolve, reject) => {
        if (!this.active) {
          resolve(null);
          return;
        }
        const timer = setTimeout(() => {
          if (this.pendingWaiter) {
            this.pendingWaiter = null;
            reject(
              new Error(
                "Security verification timed out. Please complete the Turnstile check and try again."
              )
            );
          }
        }, timeoutMs);

        this.pendingWaiter = { resolve, reject, timer };
      });
    } finally {
      this.activeAcquires = Math.max(0, this.activeAcquires - 1);
    }
  }

  reset(): void {
    if (this.pendingWaiter !== null || this.activeAcquires > 0) {
      return;
    }
    this.cachedToken = null;
    this.tokenConsumed = false;
    this.lastError = null;
    if (this.api && this.widgetId !== null) {
      this.api.reset(this.widgetId);
    }
  }

  private removeWidgetOnly(): void {
    if (this.pendingWaiter) {
      const waiter = this.pendingWaiter;
      this.pendingWaiter = null;
      if (waiter.timer !== null) clearTimeout(waiter.timer);
      waiter.resolve(null);
    }
    if (this.api && this.widgetId !== null) {
      this.api.remove(this.widgetId);
    }
    this.widgetId = null;
    this.renderedSitekey = null;
    this.cachedToken = null;
    this.tokenConsumed = false;
  }

  unmount(): void {
    this.active = false;
    this.sitekey = null;
    this.lastError = null;
    this.removeWidgetOnly();
    this.setContainerVisible(false);
  }
}
