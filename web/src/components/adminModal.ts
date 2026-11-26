import {
  createAdminInvite,
  fetchAdminUsage,
  fetchAdminUsers,
  revokeAdminInvite,
  updateAdminSettings,
  updateAdminUser,
} from "../api";
import { TrapFocusHelper } from "../modalFocus";
import { escapeHtml, formatDuration } from "../presets";
import type {
  AccountRole,
  AdminInviteRecord,
  AdminUsageEvent,
  AdminUsageResponse,
  AdminUsageRoleRow,
  AdminUserRecord,
  CreateInviteResponse,
} from "../types";

export interface AdminModalCallbacks {
  getCurrentUserId: () => string | null;
  onSettingsChanged: () => void;
  onCopyText: (text: string, label: string) => void;
  onToast: (message: string, kind: "success" | "error" | "info") => void;
  onApiError?: (err: unknown) => void;
}

type AdminTab = "usage" | "invites" | "users";

function formatShortDate(iso?: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Date(ms).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function inviteStatusLabel(inv: AdminInviteRecord): {
  label: string;
  cls: string;
  revocable: boolean;
} {
  if (inv.status === "revoked" || inv.revoked_at) {
    return { label: "Revoked", cls: "failed", revocable: false };
  }
  if (inv.status === "used" || inv.used_at || inv.accepted_at) {
    return { label: "Used", cls: "succeeded", revocable: false };
  }
  const expMs = Date.parse(inv.expires_at);
  if (
    inv.status === "expired" ||
    (!Number.isNaN(expMs) && expMs <= Date.now())
  ) {
    return { label: "Expired", cls: "cancelled", revocable: false };
  }
  return { label: "Pending", cls: "running", revocable: true };
}

function formatEventDetail(ev: AdminUsageEvent): string {
  const parts: string[] = [];
  if (ev.job_id) {
    parts.push(`#${ev.job_id.slice(0, 8)}`);
  }
  if (ev.endpoint) {
    parts.push(ev.endpoint);
  }
  if (typeof ev.status_code === "number") {
    parts.push(`HTTP ${ev.status_code}`);
  }
  if (typeof ev.detail === "string" && ev.detail.trim()) {
    parts.push(ev.detail.trim());
  } else if (ev.detail && typeof ev.detail === "object") {
    const entries = Object.entries(ev.detail)
      .filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
    if (entries.length > 0) {
      parts.push(entries.join(", "));
    }
  }
  return parts.join(" · ") || "—";
}

function parseOptionalLimit(raw: string): number | null | "invalid" {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  if (!Number.isInteger(n) || n < 0) return "invalid";
  return n;
}

export class AdminModal {
  private readonly root: HTMLElement;
  private readonly panelEl: HTMLElement;
  private readonly killSwitchBannerEl: HTMLElement;
  private readonly tabsEl: HTMLElement;
  private readonly contentEl: HTMLElement;
  private readonly callbacks: AdminModalCallbacks;
  private readonly focusTrap: TrapFocusHelper;

  private isOpen = false;
  private activeTab: AdminTab = "usage";
  private loading = false;
  private savingSettings = false;
  private creatingInvite = false;
  private busyInviteIds = new Set<string>();
  private busyUserIds = new Set<string>();

  private usage: AdminUsageResponse | null = null;
  private users: AdminUserRecord[] = [];
  private invites: AdminInviteRecord[] = [];
  private publicPaused = false;
  private lastCreatedInvite: CreateInviteResponse | null = null;
  private loadError: string | null = null;

  private inviteEmailVal = "";
  private inviteRoleVal: AccountRole = "friend";
  private inviteExpiresVal = "72";

  constructor(container: HTMLElement, callbacks: AdminModalCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "gf-modal-backdrop hidden";
    this.root.setAttribute("role", "dialog");
    this.root.setAttribute("aria-modal", "true");
    this.root.setAttribute("aria-labelledby", "gf-admin-modal-title");

    this.root.innerHTML = `
      <div class="gf-modal-panel gf-modal-panel-lg" data-role="modal-panel">
        <div class="gf-modal-header">
          <div class="gf-modal-title-group">
            <span class="gf-section-index">ADMIN</span>
            <h2 class="gf-modal-title" id="gf-admin-modal-title">Admin Dashboard</h2>
          </div>
          <div class="gf-drawer-actions">
            <button type="button" class="gf-icon-btn" data-role="admin-refresh" aria-label="Refresh admin data" title="Refresh admin data">↻</button>
            <button type="button" class="gf-icon-btn" data-role="modal-close" aria-label="Close admin dashboard" title="Close (Esc)">×</button>
          </div>
        </div>

        <div class="gf-modal-body">
          <div class="gf-admin-killswitch" data-role="admin-killswitch"></div>

          <div class="gf-segmented gf-admin-tabs" data-role="admin-tabs" role="tablist" aria-label="Admin sections">
            <button type="button" class="gf-seg-btn active" role="tab" aria-selected="true" data-admin-tab="usage">Overview &amp; Usage</button>
            <button type="button" class="gf-seg-btn" role="tab" aria-selected="false" data-admin-tab="invites">Invites</button>
            <button type="button" class="gf-seg-btn" role="tab" aria-selected="false" data-admin-tab="users">Users</button>
          </div>

          <div class="gf-admin-content" data-role="admin-content"></div>
        </div>
      </div>
    `;

    this.panelEl = this.q("[data-role='modal-panel']");
    this.killSwitchBannerEl = this.q("[data-role='admin-killswitch']");
    this.tabsEl = this.q("[data-role='admin-tabs']");
    this.contentEl = this.q("[data-role='admin-content']");
    this.focusTrap = new TrapFocusHelper(this.panelEl);

    this.bindEvents();
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`AdminModal element not found: ${selector}`);
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

    this.q("[data-role='admin-refresh']").addEventListener("click", () => {
      void this.refreshAll();
    });

    this.tabsEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("[data-admin-tab]");
      if (!btn) return;
      const tab = btn.dataset.adminTab as AdminTab;
      if (!tab || tab === this.activeTab) return;
      this.activeTab = tab;
      this.tabsEl
        .querySelectorAll<HTMLButtonElement>("[data-admin-tab]")
        .forEach((b) => {
          const active = b === btn;
          b.classList.toggle("active", active);
          b.setAttribute("aria-selected", String(active));
        });
      this.renderContent();
    });

    this.killSwitchBannerEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const sw = target?.closest<HTMLButtonElement>("[data-role='kill-switch-btn']");
      if (!sw || this.savingSettings) return;
      void this.handleToggleKillSwitch();
    });

    this.contentEl.addEventListener("input", (e) => {
      const target = e.target as HTMLInputElement | HTMLSelectElement | null;
      if (!target) return;
      if (target.id === "gf-admin-inv-email") {
        this.inviteEmailVal = target.value;
      } else if (target.id === "gf-admin-inv-role") {
        this.inviteRoleVal = target.value as AccountRole;
      } else if (target.id === "gf-admin-inv-hours") {
        this.inviteExpiresVal = target.value;
      }
    });

    this.contentEl.addEventListener("submit", (e) => {
      const form = e.target as HTMLFormElement | null;
      if (!form) return;
      if (form.dataset.role === "admin-invite-form") {
        e.preventDefault();
        void this.handleCreateInvite();
      }
    });

    this.contentEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-admin-action]");
      if (!btn) return;
      const action = btn.dataset.adminAction;

      if (action === "copy-invite-link") {
        const url = btn.dataset.inviteUrl;
        if (url) {
          this.callbacks.onCopyText(url, "Invite link copied to clipboard");
        }
      } else if (action === "revoke-invite") {
        const inviteId = btn.dataset.inviteId;
        if (inviteId) void this.handleRevokeInvite(inviteId);
      } else if (action === "save-user") {
        const userId = btn.dataset.userId;
        if (userId) void this.handleSaveUserLimits(userId);
      } else if (action === "toggle-user-disabled") {
        const userId = btn.dataset.userId;
        const nextDisabled = btn.dataset.nextDisabled === "true";
        if (userId) void this.handleToggleUserDisabled(userId, nextDisabled);
      }
    });
  }

  get opened(): boolean {
    return this.isOpen;
  }

  syncPublicPaused(paused: boolean): void {
    this.publicPaused = paused;
    if (this.isOpen) {
      this.renderKillSwitch();
    }
  }

  open(tab: AdminTab = "usage"): void {
    this.activeTab = tab;
    this.tabsEl
      .querySelectorAll<HTMLButtonElement>("[data-admin-tab]")
      .forEach((b) => {
        const active = b.dataset.adminTab === tab;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", String(active));
      });
    this.isOpen = true;
    this.root.classList.remove("hidden");
    this.renderKillSwitch();
    this.renderContent();
    this.focusTrap.activate(this.q("[data-role='modal-close']"));
    void this.refreshAll();
  }

  close(): void {
    if (!this.isOpen) return;
    this.isOpen = false;
    this.root.classList.add("hidden");
    this.focusTrap.deactivate();
  }

  async refreshAll(): Promise<void> {
    this.loading = true;
    this.loadError = null;
    this.renderContent();

    const [usageRes, usersRes] = await Promise.allSettled([
      fetchAdminUsage(),
      fetchAdminUsers(),
    ]);

    this.loading = false;

    const errors: string[] = [];

    if (usageRes.status === "fulfilled") {
      this.usage = usageRes.value;
      this.publicPaused = usageRes.value.summary.public_renders_paused;
    } else {
      const msg =
        usageRes.reason instanceof Error
          ? usageRes.reason.message
          : "Failed to load admin usage";
      errors.push(msg);
      this.callbacks.onApiError?.(usageRes.reason);
    }

    if (usersRes.status === "fulfilled") {
      this.users = usersRes.value.users;
      this.invites = usersRes.value.invites;
    } else {
      const msg =
        usersRes.reason instanceof Error
          ? usersRes.reason.message
          : "Failed to load users and invites";
      if (!errors.includes(msg)) {
        errors.push(msg);
      }
      this.callbacks.onApiError?.(usersRes.reason);
    }

    if (errors.length > 0) {
      this.loadError = errors.join(" · ");
      this.callbacks.onToast(this.loadError, "error");
    }

    this.renderKillSwitch();
    this.renderContent();
  }

  private async handleToggleKillSwitch(): Promise<void> {
    const nextPaused = !this.publicPaused;
    this.savingSettings = true;
    this.renderKillSwitch();
    try {
      const res = await updateAdminSettings({
        public_renders_paused: nextPaused,
      });
      this.publicPaused = res.public_renders_paused;
      if (this.usage) {
        this.usage = {
          ...this.usage,
          summary: {
            ...this.usage.summary,
            public_renders_paused: res.public_renders_paused,
          },
        };
      }
      this.callbacks.onSettingsChanged();
      this.callbacks.onToast(
        res.public_renders_paused
          ? "Guest (public) renders PAUSED (Friend and Admin renders remain active)"
          : "Public renders RESUMED",
        "info"
      );
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to update settings";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err);
    } finally {
      this.savingSettings = false;
      this.renderKillSwitch();
    }
  }

  private async handleCreateInvite(): Promise<void> {
    if (this.creatingInvite) return;
    const hours = Number(this.inviteExpiresVal.trim() || "72");
    if (!Number.isFinite(hours) || hours < 1 || hours > 8760) {
      this.callbacks.onToast("Expiry must be between 1 and 8760 hours", "error");
      return;
    }

    this.creatingInvite = true;
    this.renderContent();

    try {
      const res = await createAdminInvite({
        email: this.inviteEmailVal.trim() || undefined,
        role: this.inviteRoleVal,
        expires_hours: Math.round(hours),
      });
      this.lastCreatedInvite = res;
      this.inviteEmailVal = "";
      this.callbacks.onToast("Invite created", "success");
      await this.refreshAll();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to create invite";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err);
    } finally {
      this.creatingInvite = false;
      this.renderContent();
    }
  }

  private async handleRevokeInvite(inviteId: string): Promise<void> {
    if (this.busyInviteIds.has(inviteId)) return;
    this.busyInviteIds.add(inviteId);
    this.renderContent();

    try {
      await revokeAdminInvite(inviteId);
      this.callbacks.onToast("Invite revoked", "info");
      await this.refreshAll();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to revoke invite";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err);
    } finally {
      this.busyInviteIds.delete(inviteId);
      this.renderContent();
    }
  }

  private async handleSaveUserLimits(userId: string): Promise<void> {
    if (this.busyUserIds.has(userId)) return;
    const row = this.contentEl.querySelector<HTMLElement>(
      `[data-user-row="${CSS.escape(userId)}"]`
    );
    const dailyInput = row?.querySelector<HTMLInputElement>(
      "[data-user-field='daily']"
    );
    const prevInput = row?.querySelector<HTMLInputElement>(
      "[data-user-field='preview']"
    );

    const dailyParsed = parseOptionalLimit(dailyInput?.value ?? "");
    const prevParsed = parseOptionalLimit(prevInput?.value ?? "");
    if (dailyParsed === "invalid" || prevParsed === "invalid") {
      this.callbacks.onToast(
        "Quota overrides must be non-negative whole numbers or blank for default",
        "error"
      );
      return;
    }

    this.busyUserIds.add(userId);
    this.renderContent();

    try {
      const res = await updateAdminUser(userId, {
        daily_render_limit: dailyParsed,
        hourly_preview_limit: prevParsed,
      });
      this.users = this.users.map((u) =>
        u.user_id === userId ? res.user : u
      );
      this.callbacks.onSettingsChanged();
      this.callbacks.onToast(`Updated quotas for ${res.user.email}`, "success");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to update user";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err);
    } finally {
      this.busyUserIds.delete(userId);
      this.renderContent();
    }
  }

  private async handleToggleUserDisabled(
    userId: string,
    nextDisabled: boolean
  ): Promise<void> {
    if (this.busyUserIds.has(userId)) return;
    this.busyUserIds.add(userId);
    this.renderContent();

    try {
      const res = await updateAdminUser(userId, { disabled: nextDisabled });
      this.users = this.users.map((u) =>
        u.user_id === userId ? res.user : u
      );
      this.callbacks.onSettingsChanged();
      this.callbacks.onToast(
        `${res.user.email} ${res.user.disabled ? "disabled" : "enabled"}`,
        "info"
      );
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to update user status";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err);
    } finally {
      this.busyUserIds.delete(userId);
      this.renderContent();
    }
  }

  private renderKillSwitch(): void {
    const paused = this.publicPaused;
    this.killSwitchBannerEl.className = `gf-admin-killswitch${paused ? " is-paused" : ""}`;
    this.killSwitchBannerEl.innerHTML = `
      <div class="gf-admin-killswitch-info">
        <span class="gf-badge ${paused ? "failed" : "succeeded"}">${paused ? "PUBLIC PAUSED" : "PUBLIC ACTIVE"}</span>
        <span class="gf-admin-killswitch-text">
          ${paused ? "Guest (public) render submissions are paused. Friend and Admin renders stay enabled." : "Guest and Friend renders are active within configured tier quotas."}
        </span>
      </div>
      <button
        type="button"
        class="gf-btn-secondary ${paused ? "gf-admin-resume-btn" : "danger"}"
        role="switch"
        aria-checked="${paused}"
        data-role="kill-switch-btn"
        ${this.savingSettings ? "disabled" : ""}
      >
        ${this.savingSettings ? "Updating…" : paused ? "Resume Public Renders" : "Pause Public Renders"}
      </button>
    `;
  }

  private renderContent(): void {
    if (this.loading && !this.usage && this.users.length === 0) {
      this.contentEl.innerHTML = `<div class="gf-wp-empty">Loading admin telemetry, users, and invites…</div>`;
      return;
    }

    const errorBannerHtml = this.loadError
      ? `<div class="gf-form-error" role="alert">${escapeHtml(this.loadError)}</div>`
      : "";

    if (this.loadError && !this.usage && this.users.length === 0) {
      this.contentEl.innerHTML = errorBannerHtml;
      return;
    }

    if (this.activeTab === "usage") {
      this.renderUsageTab();
    } else if (this.activeTab === "invites") {
      this.renderInvitesTab();
    } else {
      this.renderUsersTab();
    }

    if (errorBannerHtml) {
      this.contentEl.insertAdjacentHTML("afterbegin", errorBannerHtml);
    }
  }

  private renderUsageTab(): void {
    if (!this.usage) {
      this.contentEl.innerHTML = `<div class="gf-wp-empty">Usage telemetry unavailable.</div>`;
      return;
    }

    const s = this.usage.summary;
    const budgetText =
      s.global_daily_public_limit === null
        ? `${s.global_daily_public_used} / ∞`
        : `${s.global_daily_public_used} / ${s.global_daily_public_limit}`;

    const roleEntries = Object.entries(this.usage.by_role ?? {});
    const roleStripHtml =
      roleEntries.length > 0
        ? roleEntries
            .map(([roleKey, val]) => {
              let detail = "";
              if (typeof val === "number") {
                detail = `${val} events`;
              } else {
                const row = val as AdminUsageRoleRow;
                const parts: string[] = [];
                if (typeof row.renders_today === "number") {
                  parts.push(`${row.renders_today} today`);
                }
                if (typeof row.renders_7d === "number") {
                  parts.push(`${row.renders_7d} 7d`);
                }
                if (typeof row.previews_today === "number") {
                  parts.push(`${row.previews_today} previews`);
                }
                detail = parts.join(" · ") || "0";
              }
              return `
                <div class="gf-admin-role-chip">
                  <span class="gf-admin-role-key">${escapeHtml(roleKey)}</span>
                  <span class="mono">${escapeHtml(detail)}</span>
                </div>
              `;
            })
            .join("")
        : `<span class="gf-sublabel">No role activity recorded yet.</span>`;

    const events = this.usage.recent_events ?? [];
    const eventsHtml =
      events.length > 0
        ? `
          <div class="gf-admin-table-wrap">
            <table class="gf-admin-table">
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Role</th>
                  <th scope="col">Event</th>
                  <th scope="col">Status / Detail</th>
                </tr>
              </thead>
              <tbody>
                ${events
                  .slice(0, 40)
                  .map((ev: AdminUsageEvent) => {
                    const roleText = String(ev.actor_role ?? ev.role ?? "—");
                    const detailText = formatEventDetail(ev);
                    return `
                      <tr>
                        <td class="mono">${escapeHtml(formatShortDate(ev.created_at))}</td>
                        <td>${escapeHtml(roleText)}</td>
                        <td class="mono">${escapeHtml(ev.event_type)}</td>
                        <td class="mono">${escapeHtml(detailText)}</td>
                      </tr>
                    `;
                  })
                  .join("")}
              </tbody>
            </table>
          </div>
        `
        : `<div class="gf-wp-empty">No recent usage events recorded.</div>`;

    this.contentEl.innerHTML = `
      <div class="gf-admin-stats-grid">
        <div class="gf-admin-stat">
          <span class="gf-admin-stat-label">Renders Today</span>
          <span class="gf-admin-stat-val mono">${s.renders_today}</span>
        </div>
        <div class="gf-admin-stat">
          <span class="gf-admin-stat-label">Renders (7d)</span>
          <span class="gf-admin-stat-val mono">${s.renders_7d}</span>
        </div>
        <div class="gf-admin-stat">
          <span class="gf-admin-stat-label">Previews Today</span>
          <span class="gf-admin-stat-val mono">${s.previews_today}</span>
        </div>
        <div class="gf-admin-stat">
          <span class="gf-admin-stat-label">Active Queue</span>
          <span class="gf-admin-stat-val mono">${s.active_queue_depth}</span>
        </div>
        <div class="gf-admin-stat">
          <span class="gf-admin-stat-label">Public Budget</span>
          <span class="gf-admin-stat-val mono">${escapeHtml(budgetText)}</span>
        </div>
        <div class="gf-admin-stat">
          <span class="gf-admin-stat-label">Avg Elapsed</span>
          <span class="gf-admin-stat-val mono">${escapeHtml(formatDuration(s.avg_elapsed_seconds))}</span>
        </div>
        <div class="gf-admin-stat">
          <span class="gf-admin-stat-label">Max Peak RSS</span>
          <span class="gf-admin-stat-val mono">${typeof s.max_peak_rss_mb === "number" ? `${Math.round(s.max_peak_rss_mb)} MB` : "—"}</span>
        </div>
      </div>

      <section class="gf-modal-section">
        <div class="gf-modal-section-head">
          <h3>Activity By Role</h3>
        </div>
        <div class="gf-admin-role-strip">${roleStripHtml}</div>
      </section>

      <section class="gf-modal-section">
        <div class="gf-modal-section-head">
          <h3>Recent Events</h3>
          <span class="gf-sublabel">${events.length} shown</span>
        </div>
        ${eventsHtml}
      </section>
    `;
  }

  private renderInvitesTab(): void {
    const createdCard = this.lastCreatedInvite
      ? (() => {
          const invitePath = this.lastCreatedInvite.invite_path?.startsWith("/")
            ? this.lastCreatedInvite.invite_path
            : `/?invite=${encodeURIComponent(this.lastCreatedInvite.token)}`;
          const fullUrl = `${window.location.origin}${invitePath}`;
          return `
            <div class="gf-admin-invite-created">
              <div class="gf-admin-invite-created-head">
                <span class="gf-badge succeeded">INVITE READY</span>
                <span class="gf-sublabel mono">Expires ${escapeHtml(formatShortDate(this.lastCreatedInvite.expires_at))}</span>
              </div>
              <div class="gf-input-group">
                <input type="text" class="gf-input mono" readonly value="${escapeHtml(fullUrl)}" aria-label="Generated invite URL" />
                <button type="button" class="gf-btn-secondary" data-admin-action="copy-invite-link" data-invite-url="${escapeHtml(fullUrl)}">Copy Link</button>
              </div>
            </div>
          `;
        })()
      : "";

    const invitesHtml =
      this.invites.length > 0
        ? this.invites
            .map((inv) => {
              const st = inviteStatusLabel(inv);
              const busy = this.busyInviteIds.has(inv.invite_id);
              return `
                <div class="gf-pack-card">
                  <div class="gf-pack-card-main">
                    <div class="gf-pack-card-title">${escapeHtml(inv.email || "Any email (open invite)")}</div>
                    <div class="gf-pack-card-meta mono">
                      Role: ${escapeHtml(inv.role)} · Expires: ${escapeHtml(formatShortDate(inv.expires_at))}
                    </div>
                  </div>
                  <div class="gf-pack-card-actions">
                    <span class="gf-badge ${st.cls}">${st.label}</span>
                    ${
                      st.revocable
                        ? `<button type="button" class="gf-mini-btn danger" data-admin-action="revoke-invite" data-invite-id="${escapeHtml(inv.invite_id)}" ${busy ? "disabled" : ""}>${busy ? "…" : "Revoke"}</button>`
                        : ""
                    }
                  </div>
                </div>
              `;
            })
            .join("")
        : `<div class="gf-wp-empty">No invites issued yet.</div>`;

    this.contentEl.innerHTML = `
      <form class="gf-admin-invite-form" data-role="admin-invite-form">
        <div class="gf-field">
          <label class="gf-label" for="gf-admin-inv-email">Email (optional)</label>
          <input
            id="gf-admin-inv-email"
            class="gf-input"
            type="email"
            value="${escapeHtml(this.inviteEmailVal)}"
            placeholder="friend@example.com"
          />
        </div>
        <div class="gf-field">
          <label class="gf-label" for="gf-admin-inv-role">Role</label>
          <select id="gf-admin-inv-role" class="gf-select">
            <option value="friend" ${this.inviteRoleVal === "friend" ? "selected" : ""}>Friend</option>
            <option value="admin" ${this.inviteRoleVal === "admin" ? "selected" : ""}>Admin</option>
          </select>
        </div>
        <div class="gf-field">
          <label class="gf-label" for="gf-admin-inv-hours">Expires (hrs)</label>
          <input
            id="gf-admin-inv-hours"
            class="gf-input mono"
            type="number"
            min="1"
            max="8760"
            value="${escapeHtml(this.inviteExpiresVal)}"
          />
        </div>
        <button type="submit" class="gf-btn-secondary gf-admin-create-invite-btn" ${this.creatingInvite ? "disabled" : ""}>
          ${this.creatingInvite ? "Creating…" : "Create Invite"}
        </button>
      </form>

      ${createdCard}

      <section class="gf-modal-section">
        <div class="gf-modal-section-head">
          <h3>Issued Invites</h3>
          <span class="gf-sublabel">${this.invites.length} total</span>
        </div>
        <div class="gf-pack-installed-list">${invitesHtml}</div>
      </section>
    `;
  }

  private renderUsersTab(): void {
    const currentUserId = this.callbacks.getCurrentUserId();
    if (this.users.length === 0) {
      this.contentEl.innerHTML = this.loadError
        ? ""
        : `<div class="gf-wp-empty">No user accounts registered yet.</div>`;
      return;
    }

    const rowsHtml = this.users
      .map((u) => {
        const isSelf = currentUserId !== null && u.user_id === currentUserId;
        const busy = this.busyUserIds.has(u.user_id);
        const dailyVal =
          u.daily_render_limit === null ? "" : String(u.daily_render_limit);
        const prevVal =
          u.hourly_preview_limit === null ? "" : String(u.hourly_preview_limit);
        const displayName = u.display_name?.trim();
        return `
          <div class="gf-admin-user-row" data-user-row="${escapeHtml(u.user_id)}">
            <div class="gf-admin-user-info">
              <div class="gf-admin-user-head">
                <span class="gf-pack-card-title">${escapeHtml(displayName ? `${displayName} (${u.email})` : u.email)}</span>
                <span class="gf-badge ${u.role === "admin" ? "running" : "succeeded"}">${escapeHtml(u.role)}</span>
                ${u.disabled ? `<span class="gf-badge failed">disabled</span>` : ""}
                ${isSelf ? `<span class="gf-sublabel mono">(you)</span>` : ""}
              </div>
              <div class="gf-pack-card-meta mono">
                Created ${escapeHtml(formatShortDate(u.created_at))}${typeof u.renders_today === "number" ? ` · ${u.renders_today} renders today` : ""}
              </div>
            </div>
            <div class="gf-admin-user-controls">
              <label class="gf-admin-limit-field">
                <span>Daily</span>
                <input
                  type="number"
                  min="0"
                  class="gf-input mono"
                  data-user-field="daily"
                  value="${escapeHtml(dailyVal)}"
                  placeholder="Default"
                  aria-label="Daily render limit for ${escapeHtml(u.email)}"
                />
              </label>
              <label class="gf-admin-limit-field">
                <span>Hourly</span>
                <input
                  type="number"
                  min="0"
                  class="gf-input mono"
                  data-user-field="preview"
                  value="${escapeHtml(prevVal)}"
                  placeholder="Default"
                  aria-label="Hourly preview limit for ${escapeHtml(u.email)}"
                />
              </label>
              <button
                type="button"
                class="gf-mini-btn"
                data-admin-action="save-user"
                data-user-id="${escapeHtml(u.user_id)}"
                ${busy ? "disabled" : ""}
              >${busy ? "…" : "Save"}</button>
              <button
                type="button"
                class="gf-mini-btn ${u.disabled ? "" : "danger"}"
                data-admin-action="toggle-user-disabled"
                data-user-id="${escapeHtml(u.user_id)}"
                data-next-disabled="${String(!u.disabled)}"
                ${busy || isSelf ? "disabled" : ""}
                title="${isSelf ? "Cannot disable your own admin account" : u.disabled ? "Enable account" : "Disable account"}"
              >${u.disabled ? "Enable" : "Disable"}</button>
            </div>
          </div>
        `;
      })
      .join("");

    this.contentEl.innerHTML = `
      <section class="gf-modal-section">
        <div class="gf-modal-section-head">
          <h3>Accounts &amp; Quota Overrides</h3>
          <span class="gf-sublabel">Leave blank for role default</span>
        </div>
        <div class="gf-pack-installed-list">${rowsHtml}</div>
      </section>
    `;
  }
}
