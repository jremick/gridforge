import {
  acceptInvite,
  googleAuthStartUrl,
  loginWithPassword,
  logoutSession,
} from "../api";
import { ICON_CLOSE } from "../icons";
import { TrapFocusHelper } from "../modalFocus";
import { escapeHtml } from "../presets";
import { initModalBackdrop } from "./lifecycle";
import {
  createDefaultNoneSession,
  formatUploadLimitLabel,
} from "../session";
import type { SessionResponse } from "../types";

export type AuthModalMode = "signin" | "invite" | "account";

export interface AuthModalCallbacks {
  changeSession: (
    request: () => Promise<SessionResponse>
  ) => Promise<SessionResponse>;
  onOpenAdmin: () => void;
  onToast: (message: string, kind: "success" | "error" | "info") => void;
}

export interface AuthModalOpenOptions {
  mode?: AuthModalMode;
  inviteToken?: string | null;
  bannerError?: string | null;
}

export class AuthModal {
  private readonly root: HTMLElement;
  private readonly panelEl: HTMLElement;
  private readonly titleEl: HTMLElement;
  private readonly bodyEl: HTMLElement;
  private readonly callbacks: AuthModalCallbacks;
  private readonly focusTrap: TrapFocusHelper;

  private isOpen = false;
  private mode: AuthModalMode = "signin";
  private session: SessionResponse = createDefaultNoneSession();
  private submitting = false;
  private errorMessage: string | null = null;
  private invalidFieldId: string | null = null;

  private signinEmail = "";
  private inviteToken = "";
  private inviteEmail = "";
  private inviteName = "";

  constructor(container: HTMLElement, callbacks: AuthModalCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    initModalBackdrop(this.root, "gf-auth-modal-title");

    this.root.innerHTML = `
      <div class="gf-modal-panel gf-modal-panel-sm" data-role="modal-panel">
        <div class="gf-modal-header">
          <div class="gf-modal-title-group">
            <h2 class="gf-modal-title" id="gf-auth-modal-title" data-role="auth-title">Sign In</h2>
          </div>
          <div class="gf-drawer-actions">
            <button type="button" class="gf-icon-btn" data-role="modal-close" aria-label="Close dialog" title="Close (Esc)">${ICON_CLOSE}</button>
          </div>
        </div>
        <div class="gf-modal-body gf-auth-body" data-role="auth-body"></div>
      </div>
    `;

    this.panelEl = this.q("[data-role='modal-panel']");
    this.titleEl = this.q("[data-role='auth-title']");
    this.bodyEl = this.q("[data-role='auth-body']");
    this.focusTrap = new TrapFocusHelper(this.panelEl);

    this.bindEvents();
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`AuthModal element not found: ${selector}`);
    return el;
  }

  private bindEvents(): void {
    this.root.addEventListener("click", (e) => {
      if (e.target === this.root) {
        this.close();
      }
    });

    this.q("[data-role='modal-close']").addEventListener("click", () => {
      this.close();
    });

    this.bodyEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;

      const googleInviteLink = target?.closest<HTMLAnchorElement>(
        "[data-role='google-invite-link']"
      );
      if (googleInviteLink) {
        const token = this.inviteToken.trim();
        if (!token) {
          e.preventDefault();
          this.setError(
            "Enter your invite token before continuing with Google.",
            "gf-invite-token"
          );
          const input =
            this.bodyEl.querySelector<HTMLInputElement>("#gf-invite-token");
          input?.focus();
          return;
        }
        googleInviteLink.href = googleAuthStartUrl(token);
        return;
      }

      const actionBtn = target?.closest<HTMLButtonElement>("[data-action]");
      if (!actionBtn) return;
      const action = actionBtn.dataset.action;
      if (action === "to-invite") {
        this.errorMessage = null;
        this.invalidFieldId = null;
        this.mode = "invite";
        this.renderBody();
        this.focusInitialInput();
      } else if (action === "to-signin") {
        this.errorMessage = null;
        this.invalidFieldId = null;
        this.mode = "signin";
        this.renderBody();
        this.focusInitialInput();
      } else if (action === "open-admin") {
        this.close();
        this.callbacks.onOpenAdmin();
      } else if (action === "signout") {
        void this.handleSignOut();
      }
    });

    this.bodyEl.addEventListener("input", (e) => {
      const target = e.target as HTMLInputElement | null;
      if (!target) return;
      if (target.id === "gf-auth-email") {
        this.signinEmail = target.value;
      } else if (target.id === "gf-invite-token") {
        this.inviteToken = target.value;
        const googleLink = this.bodyEl.querySelector<HTMLAnchorElement>(
          "[data-role='google-invite-link']"
        );
        if (googleLink) {
          googleLink.href = googleAuthStartUrl(this.inviteToken);
        }
      } else if (target.id === "gf-invite-email") {
        this.inviteEmail = target.value;
      } else if (target.id === "gf-invite-name") {
        this.inviteName = target.value;
      }

      if (this.invalidFieldId === target.id) {
        target.removeAttribute("aria-invalid");
        target.removeAttribute("aria-describedby");
        this.invalidFieldId = null;
      }
      if (this.errorMessage) {
        this.setError(null, null);
      }
    });

    this.bodyEl.addEventListener("submit", (e) => {
      const form = e.target as HTMLFormElement | null;
      if (!form) return;
      e.preventDefault();
      if (form.dataset.role === "signin-form") {
        void this.handleSignInSubmit(form);
      } else if (form.dataset.role === "invite-form") {
        void this.handleInviteSubmit(form);
      }
    });
  }

  get opened(): boolean {
    return this.isOpen;
  }

  setSession(session: SessionResponse): void {
    const prev = this.session;
    this.session = session;
    if (!this.isOpen) return;

    const prevMode = this.mode;
    if (session.authenticated && this.mode !== "account") {
      this.mode = "account";
      this.errorMessage = null;
      this.invalidFieldId = null;
    } else if (!session.authenticated && this.mode === "account") {
      this.mode = "signin";
    }

    const viewStateChanged =
      this.mode !== prevMode ||
      this.mode === "account" ||
      prev.authenticated !== session.authenticated ||
      prev.role !== session.role ||
      (prev.user?.user_id ?? null) !== (session.user?.user_id ?? null) ||
      prev.capabilities.google_oauth_enabled !==
        session.capabilities.google_oauth_enabled;

    if (viewStateChanged) {
      this.renderBody();
    }
  }

  open(options: AuthModalOpenOptions = {}): void {
    if (options.inviteToken !== undefined && options.inviteToken !== null) {
      this.inviteToken = options.inviteToken;
    }

    if (options.mode) {
      this.mode = options.mode;
    } else if (this.session.authenticated) {
      this.mode = "account";
    } else if (this.inviteToken.trim()) {
      this.mode = "invite";
    } else {
      this.mode = "signin";
    }

    this.errorMessage = options.bannerError ?? null;
    this.invalidFieldId = null;
    this.submitting = false;
    this.isOpen = true;
    this.root.classList.remove("hidden");
    this.renderBody();
    this.focusTrap.activate(this.getInitialFocusTarget());
  }

  close(): void {
    if (!this.isOpen) return;
    this.isOpen = false;
    this.submitting = false;
    this.root.classList.add("hidden");
    this.focusTrap.deactivate();
  }

  clearAccountState(): void {
    this.close();
    this.signinEmail = "";
    this.inviteToken = "";
    this.inviteEmail = "";
    this.inviteName = "";
    this.errorMessage = null;
    this.invalidFieldId = null;
    this.bodyEl.replaceChildren();
  }

  private getInitialFocusTarget(): HTMLElement | null {
    if (this.mode === "signin") {
      return this.bodyEl.querySelector<HTMLElement>("#gf-auth-email");
    }
    if (this.mode === "invite") {
      if (this.inviteToken.trim()) {
        return (
          this.bodyEl.querySelector<HTMLElement>("#gf-invite-password") ??
          this.bodyEl.querySelector<HTMLElement>("#gf-invite-token")
        );
      }
      return this.bodyEl.querySelector<HTMLElement>("#gf-invite-token");
    }
    return this.q("[data-role='modal-close']");
  }

  private focusInitialInput(): void {
    const target = this.getInitialFocusTarget();
    target?.focus();
  }

  private setError(message: string | null, fieldId: string | null): void {
    this.errorMessage = message;
    this.invalidFieldId = fieldId;

    const errEl = this.bodyEl.querySelector<HTMLElement>("[data-role='auth-error']");
    if (errEl) {
      if (message) {
        errEl.textContent = message;
        errEl.classList.remove("hidden");
      } else {
        errEl.textContent = "";
        errEl.classList.add("hidden");
      }
    }

    this.bodyEl
      .querySelectorAll<HTMLInputElement>("input[aria-invalid]")
      .forEach((el) => {
        el.removeAttribute("aria-invalid");
        el.removeAttribute("aria-describedby");
      });

    if (fieldId && errEl?.id) {
      const field = this.bodyEl.querySelector<HTMLInputElement>(`#${fieldId}`);
      if (field) {
        field.setAttribute("aria-invalid", "true");
        field.setAttribute("aria-describedby", errEl.id);
      }
    }
  }

  private setPendingState(form: HTMLFormElement, pending: boolean, label: string): void {
    this.submitting = pending;
    const btn = form.querySelector<HTMLButtonElement>("button[type='submit']");
    if (btn) {
      btn.disabled = pending;
      btn.textContent = label;
    }
  }

  private async handleSignInSubmit(form: HTMLFormElement): Promise<void> {
    if (this.submitting) return;
    const emailInput = form.querySelector<HTMLInputElement>("#gf-auth-email");
    const pwInput = form.querySelector<HTMLInputElement>("#gf-auth-password");
    const email = (emailInput?.value ?? "").trim();
    const password = pwInput?.value ?? "";
    this.signinEmail = email;

    if (!email) {
      this.setError("Enter your account email address.", "gf-auth-email");
      emailInput?.focus();
      return;
    }
    if (!password) {
      this.setError("Enter your password.", "gf-auth-password");
      pwInput?.focus();
      return;
    }

    this.setError(null, null);
    this.setPendingState(form, true, "Signing in…");

    try {
      const nextSession = await this.callbacks.changeSession(() =>
        loginWithPassword({ email, password })
      );
      const actor =
        nextSession.user?.display_name?.trim() ||
        nextSession.user?.email ||
        nextSession.role;
      this.callbacks.onToast(`Signed in as ${actor}`, "success");
      this.close();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Sign in failed";
      this.setError(msg, "gf-auth-password");
      pwInput?.focus();
    } finally {
      if (this.isOpen && this.mode === "signin") {
        this.setPendingState(form, false, "Sign In");
      } else {
        this.submitting = false;
      }
    }
  }

  private async handleInviteSubmit(form: HTMLFormElement): Promise<void> {
    if (this.submitting) return;
    const tokenInput = form.querySelector<HTMLInputElement>("#gf-invite-token");
    const emailInput = form.querySelector<HTMLInputElement>("#gf-invite-email");
    const nameInput = form.querySelector<HTMLInputElement>("#gf-invite-name");
    const pwInput = form.querySelector<HTMLInputElement>("#gf-invite-password");

    const token = (tokenInput?.value ?? "").trim();
    const email = (emailInput?.value ?? "").trim();
    const displayName = (nameInput?.value ?? "").trim();
    const password = pwInput?.value ?? "";

    this.inviteToken = token;
    this.inviteEmail = email;
    this.inviteName = displayName;

    if (!token) {
      this.setError("Enter your invite token.", "gf-invite-token");
      tokenInput?.focus();
      return;
    }
    if (password.length < 8) {
      this.setError(
        "Choose a password of at least 8 characters.",
        "gf-invite-password"
      );
      pwInput?.focus();
      return;
    }

    this.setError(null, null);
    this.setPendingState(form, true, "Activating…");

    try {
      await this.callbacks.changeSession(() =>
        acceptInvite({
          token,
          email: email || undefined,
          display_name: displayName || undefined,
          password,
        })
      );
      this.inviteToken = "";
      this.inviteEmail = "";
      this.inviteName = "";
      this.callbacks.onToast("Account activated and signed in", "success");
      this.close();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Could not redeem invite";
      this.setError(msg, "gf-invite-token");
      tokenInput?.focus();
    } finally {
      if (this.isOpen && this.mode === "invite") {
        this.setPendingState(form, false, "Activate Account");
      } else {
        this.submitting = false;
      }
    }
  }

  private async handleSignOut(): Promise<void> {
    if (this.submitting) return;
    this.submitting = true;
    const btn = this.bodyEl.querySelector<HTMLButtonElement>(
      "[data-action='signout']"
    );
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Signing out…";
    }
    try {
      await this.callbacks.changeSession(logoutSession);
      this.callbacks.onToast("Signed out", "info");
      this.close();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to sign out";
      this.setError(msg, null);
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Sign Out";
      }
    } finally {
      this.submitting = false;
    }
  }

  private renderBody(): void {
    const googleEnabled = this.session.capabilities.google_oauth_enabled;
    const errHtml = (id: string) =>
      `<div id="${id}" class="gf-form-error${this.errorMessage ? "" : " hidden"}" data-role="auth-error" role="alert">${escapeHtml(this.errorMessage ?? "")}</div>`;

    if (this.mode === "account" && this.session.authenticated) {
      const u = this.session.user;
      const q = this.session.quotas;
      const roleLabel = this.session.role === "admin" ? "Admin" : "Friend";
      const retention =
        this.session.role === "admin" ? "Unlimited retention" : "14 days";
      const dailyText =
        q.daily_renders_limit === null
          ? "Unlimited"
          : `${q.daily_renders_remaining ?? Math.max(0, q.daily_renders_limit - q.daily_renders_used)} / ${q.daily_renders_limit} left today`;
      const previewText =
        q.hourly_previews_limit === null
          ? "Unlimited"
          : `${q.hourly_previews_remaining ?? Math.max(0, q.hourly_previews_limit - q.hourly_previews_used)} / ${q.hourly_previews_limit} left this hour`;

      this.titleEl.textContent = "Account";

      this.bodyEl.innerHTML = `
        <div class="gf-auth-account-card">
          <div class="gf-auth-account-head">
            <div>
              <div class="gf-auth-account-name">${escapeHtml(u?.display_name?.trim() || u?.email || roleLabel)}</div>
              ${u?.email ? `<div class="gf-auth-account-email mono">${escapeHtml(u.email)}</div>` : ""}
            </div>
            <span class="gf-badge ${this.session.role === "admin" ? "running" : "succeeded"}">${escapeHtml(roleLabel)}</span>
          </div>
          <dl class="gf-auth-kv">
            <dt>Daily Renders</dt>
            <dd class="mono">${escapeHtml(dailyText)}</dd>
            <dt>Hourly Previews</dt>
            <dd class="mono">${escapeHtml(previewText)}</dd>
            <dt>Job &amp; Project Retention</dt>
            <dd class="mono">${escapeHtml(retention)}</dd>
            <dt>Max Upload Size</dt>
            <dd class="mono">${escapeHtml(formatUploadLimitLabel(this.session.capabilities.max_upload_bytes))}</dd>
          </dl>
        </div>
        ${errHtml("gf-auth-account-error")}
        <div class="gf-auth-account-actions">
          ${
            this.session.role === "admin"
              ? `<button type="button" class="gf-btn-secondary gf-auth-admin-btn" data-action="open-admin">Open Admin Dashboard</button>`
              : ""
          }
          <button type="button" class="gf-btn-secondary danger" data-action="signout">Sign Out</button>
        </div>
      `;
      return;
    }

    if (this.mode === "invite") {
      this.titleEl.textContent = "Redeem Invite";

      const googleSection = googleEnabled
        ? `
          <div class="gf-auth-divider"><span>or</span></div>
          <a
            class="gf-oauth-btn"
            data-role="google-invite-link"
            href="${escapeHtml(googleAuthStartUrl(this.inviteToken))}"
          >Continue with Google with invite</a>
        `
        : "";

      this.bodyEl.innerHTML = `
        <form class="gf-auth-form" data-role="invite-form" novalidate>
          <div class="gf-field">
            <label class="gf-label" for="gf-invite-token">Invite Token</label>
            <input
              id="gf-invite-token"
              class="gf-input mono"
              type="text"
              autocomplete="off"
              spellcheck="false"
              required
              value="${escapeHtml(this.inviteToken)}"
              placeholder="Paste invite token"
              ${this.invalidFieldId === "gf-invite-token" ? 'aria-invalid="true" aria-describedby="gf-auth-invite-error"' : ""}
            />
          </div>
          <div class="gf-field">
            <label class="gf-label" for="gf-invite-email">
              <span>Email</span>
              <span class="gf-sublabel">Optional if pre-bound</span>
            </label>
            <input
              id="gf-invite-email"
              class="gf-input"
              type="email"
              autocomplete="email"
              value="${escapeHtml(this.inviteEmail)}"
              placeholder="you@example.com"
            />
          </div>
          <div class="gf-field">
            <label class="gf-label" for="gf-invite-name">
              <span>Display Name</span>
              <span class="gf-sublabel">Optional</span>
            </label>
            <input
              id="gf-invite-name"
              class="gf-input"
              type="text"
              autocomplete="name"
              maxlength="80"
              value="${escapeHtml(this.inviteName)}"
              placeholder="Callsign or name"
            />
          </div>
          <div class="gf-field">
            <label class="gf-label" for="gf-invite-password">
              <span>Password</span>
              <span class="gf-sublabel">Min 8 characters</span>
            </label>
            <input
              id="gf-invite-password"
              class="gf-input"
              type="password"
              autocomplete="new-password"
              required
              minlength="8"
              placeholder="Create a password"
              ${this.invalidFieldId === "gf-invite-password" ? 'aria-invalid="true" aria-describedby="gf-auth-invite-error"' : ""}
            />
          </div>
          ${errHtml("gf-auth-invite-error")}
          <button type="submit" class="gf-generate-btn gf-auth-submit-btn">Activate Account</button>
        </form>
        ${googleSection}
        <div class="gf-auth-footer">
          <button type="button" class="gf-link-btn" data-action="to-signin">← Back to Sign In</button>
        </div>
      `;
      return;
    }

    // Sign In mode (default)
    this.titleEl.textContent = "Sign In";

    const googleSection = googleEnabled
      ? `
        <div class="gf-auth-divider"><span>or</span></div>
        <a
          class="gf-oauth-btn"
          data-role="google-signin-link"
          href="${escapeHtml(googleAuthStartUrl())}"
        >Continue with Google</a>
      `
      : "";

    this.bodyEl.innerHTML = `
      <form class="gf-auth-form" data-role="signin-form" novalidate>
        <div class="gf-field">
          <label class="gf-label" for="gf-auth-email">Email</label>
          <input
            id="gf-auth-email"
            class="gf-input"
            type="email"
            autocomplete="email"
            required
            value="${escapeHtml(this.signinEmail)}"
            placeholder="you@example.com"
            ${this.invalidFieldId === "gf-auth-email" ? 'aria-invalid="true" aria-describedby="gf-auth-signin-error"' : ""}
          />
        </div>
        <div class="gf-field">
          <label class="gf-label" for="gf-auth-password">Password</label>
          <input
            id="gf-auth-password"
            class="gf-input"
            type="password"
            autocomplete="current-password"
            required
            placeholder="Enter password"
            ${this.invalidFieldId === "gf-auth-password" ? 'aria-invalid="true" aria-describedby="gf-auth-signin-error"' : ""}
          />
        </div>
        ${errHtml("gf-auth-signin-error")}
        <button type="submit" class="gf-generate-btn gf-auth-submit-btn">Sign In</button>
      </form>
      ${googleSection}
      <div class="gf-auth-footer">
        <button type="button" class="gf-link-btn" data-action="to-invite">Have an invite token? Redeem invite →</button>
        <span class="gf-auth-note">Invite-only access. No public sign-up.</span>
      </div>
    `;
  }
}
