import {
  createProject,
  deleteProject,
  fetchProjects,
  jobThumbnailUrl,
} from "../api";
import { ICON_CLOSE, ICON_REFRESH } from "../icons";
import { escapeHtml } from "../presets";
import type { SavedProject } from "../types";
import { reportAbortAwareError } from "./lifecycle";

const PROJECT_NAME_PLACEHOLDER = "Project name (e.g. Kosciuszko Winter Loop)";

export interface ProjectsDrawerCallbacks {
  onClose: () => void;
  onLoadProject: (project: SavedProject) => void;
  onShareProject?: (project: SavedProject) => Promise<void> | void;
  getCurrentSnapshot: () => {
    suggestedName: string;
    state: Record<string, unknown>;
    last_job_id: string | null;
  };
  onToast: (message: string, kind: "success" | "error" | "info") => void;
  onApiError?: (err: unknown, context: "projects") => void;
}

function formatDateLabel(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Date(ms).toLocaleDateString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export class ProjectsDrawer {
  private readonly root: HTMLElement;
  private readonly listEl: HTMLElement;
  private readonly countEl: HTMLElement;
  private readonly nameInput: HTMLInputElement;
  private readonly notesInput: HTMLInputElement;
  private readonly saveBtn: HTMLButtonElement;
  private readonly callbacks: ProjectsDrawerCallbacks;

  private projects: SavedProject[] = [];
  private busyIds = new Set<string>();
  private saving = false;
  private requests = new AbortController();

  constructor(container: HTMLElement, callbacks: ProjectsDrawerCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "gf-drawer gf-projects-drawer";
    this.root.setAttribute("aria-label", "Saved Trip Projects");
    this.root.innerHTML = `
      <div class="gf-drawer-header">
        <div class="gf-drawer-title">
          <span>Trip Projects</span>
          <span class="gf-section-pill" data-role="projects-count">0</span>
        </div>
        <div class="gf-drawer-actions">
          <button type="button" class="gf-icon-btn" data-role="projects-refresh" aria-label="Refresh projects" title="Refresh projects">${ICON_REFRESH}</button>
          <button type="button" class="gf-icon-btn" data-role="projects-close" aria-label="Close projects drawer" title="Close projects drawer">${ICON_CLOSE}</button>
        </div>
      </div>

      <div class="gf-projects-save-bar">
        <input
          type="text"
          class="gf-input"
          data-role="proj-name"
          maxlength="80"
          placeholder="${PROJECT_NAME_PLACEHOLDER}"
        />
        <div class="gf-input-group">
          <input
            type="text"
            class="gf-input"
            data-role="proj-notes"
            maxlength="160"
            placeholder="Optional trip notes…"
          />
          <button type="button" class="gf-btn-secondary gf-proj-save-btn" data-role="proj-save">
            Save
          </button>
        </div>
      </div>

      <div class="gf-drawer-body" data-role="projects-list"></div>
    `;

    this.listEl = this.q("[data-role='projects-list']");
    this.countEl = this.q("[data-role='projects-count']");
    this.nameInput = this.q<HTMLInputElement>("[data-role='proj-name']");
    this.notesInput = this.q<HTMLInputElement>("[data-role='proj-notes']");
    this.saveBtn = this.q<HTMLButtonElement>("[data-role='proj-save']");

    this.q("[data-role='projects-close']").addEventListener("click", () => {
      this.callbacks.onClose();
    });
    this.q("[data-role='projects-refresh']").addEventListener("click", () => {
      void this.refresh();
    });
    this.saveBtn.addEventListener("click", () => {
      void this.handleSaveCurrent();
    });
    this.nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        void this.handleSaveCurrent();
      }
    });

    this.listEl.addEventListener(
      "error",
      (e) => {
        const target = e.target as HTMLElement | null;
        if (target instanceof HTMLImageElement && target.classList.contains("gf-proj-thumb")) {
          target.style.display = "none";
        }
      },
      true
    );

    this.listEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-proj-action]");
      if (!btn) return;
      const action = btn.dataset.projAction;
      const id = btn.dataset.projId;
      if (!id) return;
      const proj = this.projects.find((p) => p.project_id === id);
      if (!proj) return;

      if (action === "load") {
        this.callbacks.onLoadProject(proj);
      } else if (action === "share") {
        void this.handleShare(proj);
      } else if (action === "clone") {
        void this.handleClone(proj);
      } else if (action === "delete") {
        void this.handleDelete(id);
      }
    });

    this.renderList();
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`ProjectsDrawer element not found: ${selector}`);
    return el;
  }

  prepareSuggestedName(): void {
    if (!this.nameInput.value.trim()) {
      const snap = this.callbacks.getCurrentSnapshot();
      this.nameInput.placeholder =
        snap.suggestedName || PROJECT_NAME_PLACEHOLDER;
    }
  }

  clear(): void {
    this.requests.abort();
    this.requests = new AbortController();
    this.projects = [];
    this.busyIds.clear();
    this.nameInput.value = "";
    this.nameInput.placeholder = PROJECT_NAME_PLACEHOLDER;
    this.notesInput.value = "";
    this.setSaving(false);
    this.countEl.textContent = "0";
    this.renderList();
  }

  async refresh(): Promise<void> {
    const { signal } = this.requests;
    try {
      const projects = await fetchProjects(signal);
      if (signal.aborted) return;
      this.projects = projects;
      this.countEl.textContent = String(this.projects.length);
      this.renderList();
    } catch (err) {
      if (!signal.aborted) this.callbacks.onApiError?.(err, "projects");
    }
  }

  private setSaving(saving: boolean): void {
    this.saving = saving;
    this.saveBtn.disabled = saving;
    this.saveBtn.textContent = saving ? "Saving…" : "Save";
  }

  private async handleSaveCurrent(): Promise<void> {
    if (this.saving) return;
    const { signal } = this.requests;
    const snap = this.callbacks.getCurrentSnapshot();
    const name = this.nameInput.value.trim() || snap.suggestedName || "Untitled Trip";
    const notes = this.notesInput.value.trim();

    this.setSaving(true);

    try {
      const created = await createProject({
        name,
        description: notes,
        notes,
        state: snap.state,
        last_job_id: snap.last_job_id,
      }, signal);
      if (signal.aborted) return;
      this.projects = [
        created,
        ...this.projects.filter((p) => p.project_id !== created.project_id),
      ];
      this.nameInput.value = "";
      this.notesInput.value = "";
      this.countEl.textContent = String(this.projects.length);
      this.renderList();
      this.callbacks.onToast(`Saved project "${created.name}"`, "success");
    } catch (err) {
      reportAbortAwareError(
        signal,
        err,
        "Failed to save project",
        this.callbacks.onToast,
        (e) => this.callbacks.onApiError?.(e, "projects")
      );
    } finally {
      if (!signal.aborted) this.setSaving(false);
    }
  }

  private async handleShare(proj: SavedProject): Promise<void> {
    if (!this.callbacks.onShareProject) return;
    const { signal } = this.requests;
    this.busyIds.add(proj.project_id);
    this.renderList();
    try {
      await this.callbacks.onShareProject(proj);
    } finally {
      if (!signal.aborted) {
        this.busyIds.delete(proj.project_id);
        this.renderList();
      }
    }
  }

  private async handleClone(proj: SavedProject): Promise<void> {
    const { signal } = this.requests;
    this.busyIds.add(proj.project_id);
    this.renderList();
    try {
      const notes = (proj.description ?? proj.notes ?? "").trim();
      const cloned = await createProject({
        name: `${proj.name} (Copy)`.slice(0, 80),
        description: notes,
        notes,
        state: proj.state,
        last_job_id: proj.last_job_id ?? null,
      }, signal);
      if (signal.aborted) return;
      this.projects = [cloned, ...this.projects];
      this.countEl.textContent = String(this.projects.length);
      this.callbacks.onToast(`Cloned "${proj.name}"`, "info");
    } catch (err) {
      reportAbortAwareError(
        signal,
        err,
        "Failed to clone project",
        this.callbacks.onToast,
        (e) => this.callbacks.onApiError?.(e, "projects")
      );
    } finally {
      if (!signal.aborted) {
        this.busyIds.delete(proj.project_id);
        this.renderList();
      }
    }
  }

  private async handleDelete(id: string): Promise<void> {
    const { signal } = this.requests;
    this.busyIds.add(id);
    this.renderList();
    try {
      await deleteProject(id, signal);
      if (signal.aborted) return;
      this.projects = this.projects.filter((p) => p.project_id !== id);
      this.countEl.textContent = String(this.projects.length);
      this.callbacks.onToast("Deleted saved project", "info");
    } catch (err) {
      reportAbortAwareError(
        signal,
        err,
        "Failed to delete project",
        this.callbacks.onToast,
        (e) => this.callbacks.onApiError?.(e, "projects")
      );
    } finally {
      if (!signal.aborted) {
        this.busyIds.delete(id);
        this.renderList();
      }
    }
  }

  private renderList(): void {
    if (this.projects.length === 0) {
      this.listEl.innerHTML = `
        <div class="gf-empty-state">
          <div class="gf-empty-title">No saved trip projects</div>
          <div>Save your current sheet area, overlays, waypoints, RF links, and tactical graphics for instant reload later.</div>
        </div>
      `;
      return;
    }

    this.listEl.innerHTML = this.projects
      .map((p) => {
        const s = p.state as Record<string, unknown>;
        const scale =
          typeof s.scale === "number"
            ? `1:${s.scale.toLocaleString()}`
            : "1:25,000";
        const paper =
          typeof s.paper === "string" ? s.paper.toUpperCase() : "A4";
        const mode =
          typeof s.color_mode === "string" ? s.color_mode : "color";
        const center =
          typeof s.center === "string" ? s.center : "";
        const busy = this.busyIds.has(p.project_id);
        const thumbSrc =
          p.thumbnail_url || (p.last_job_id ? jobThumbnailUrl(p.last_job_id) : null);
        const thumbHtml = thumbSrc
          ? `<img class="gf-proj-thumb" src="${escapeHtml(thumbSrc)}" alt="" loading="lazy" />`
          : "";
        const notesText = p.description ?? p.notes ?? "";

        return `
          <article class="gf-job-card gf-proj-card">
            <div class="gf-proj-head">
              ${thumbHtml}
              <div class="gf-proj-info">
                <div class="gf-job-title">${escapeHtml(p.name)}</div>
                <div class="gf-job-sub">${escapeHtml(center)} · ${escapeHtml(scale)} · ${escapeHtml(paper)} · ${escapeHtml(mode)}</div>
                ${notesText ? `<div class="gf-proj-notes">${escapeHtml(notesText)}</div>` : ""}
              </div>
            </div>
            <div class="gf-job-footer">
              <span class="gf-job-id">${escapeHtml(formatDateLabel(p.updated_at))}</span>
              <div class="gf-job-btn-row">
                <button type="button" class="gf-mini-btn gf-proj-load-btn" data-proj-action="load" data-proj-id="${escapeHtml(p.project_id)}" ${busy ? "disabled" : ""}>Load</button>
                ${
                  this.callbacks.onShareProject
                    ? `<button type="button" class="gf-mini-btn" data-proj-action="share" data-proj-id="${escapeHtml(p.project_id)}" ${busy ? "disabled" : ""} title="Create a read-only shareable snapshot link">Share</button>`
                    : ""
                }
                <button type="button" class="gf-mini-btn" data-proj-action="clone" data-proj-id="${escapeHtml(p.project_id)}" ${busy ? "disabled" : ""}>Clone</button>
                <button type="button" class="gf-mini-btn danger" data-proj-action="delete" data-proj-id="${escapeHtml(p.project_id)}" ${busy ? "disabled" : ""}>Delete</button>
              </div>
            </div>
          </article>
        `;
      })
      .join("");
  }
}
