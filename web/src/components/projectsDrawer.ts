import {
  createProject,
  deleteProject,
  fetchProjects,
  jobThumbnailUrl,
} from "../api";
import { escapeHtml } from "../presets";
import type { SavedProject } from "../types";

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
          <button type="button" class="gf-icon-btn" data-role="projects-refresh" title="Refresh projects">↻</button>
          <button type="button" class="gf-icon-btn" data-role="projects-close" title="Close projects drawer">×</button>
        </div>
      </div>

      <div class="gf-projects-save-bar">
        <input
          type="text"
          class="gf-input"
          data-role="proj-name"
          maxlength="80"
          placeholder="Project name (e.g. Kosciuszko Winter Loop)"
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
        snap.suggestedName || "Project name (e.g. Kosciuszko Loop)";
    }
  }

  clear(): void {
    this.projects = [];
    this.countEl.textContent = "0";
    this.renderList();
  }

  async refresh(): Promise<void> {
    try {
      this.projects = await fetchProjects();
      this.countEl.textContent = String(this.projects.length);
      this.renderList();
    } catch (err) {
      this.callbacks.onApiError?.(err, "projects");
    }
  }

  private async handleSaveCurrent(): Promise<void> {
    if (this.saving) return;
    const snap = this.callbacks.getCurrentSnapshot();
    const name = this.nameInput.value.trim() || snap.suggestedName || "Untitled Trip";
    const notes = this.notesInput.value.trim();

    this.saving = true;
    this.saveBtn.disabled = true;
    this.saveBtn.textContent = "Saving…";

    try {
      const created = await createProject({
        name,
        description: notes,
        notes,
        state: snap.state,
        last_job_id: snap.last_job_id,
      });
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
      const msg = err instanceof Error ? err.message : "Failed to save project";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err, "projects");
    } finally {
      this.saving = false;
      this.saveBtn.disabled = false;
      this.saveBtn.textContent = "Save";
    }
  }

  private async handleShare(proj: SavedProject): Promise<void> {
    if (!this.callbacks.onShareProject) return;
    this.busyIds.add(proj.project_id);
    this.renderList();
    try {
      await this.callbacks.onShareProject(proj);
    } finally {
      this.busyIds.delete(proj.project_id);
      this.renderList();
    }
  }

  private async handleClone(proj: SavedProject): Promise<void> {
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
      });
      this.projects = [cloned, ...this.projects];
      this.countEl.textContent = String(this.projects.length);
      this.callbacks.onToast(`Cloned "${proj.name}"`, "info");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to clone project";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err, "projects");
    } finally {
      this.busyIds.delete(proj.project_id);
      this.renderList();
    }
  }

  private async handleDelete(id: string): Promise<void> {
    this.busyIds.add(id);
    this.renderList();
    try {
      await deleteProject(id);
      this.projects = this.projects.filter((p) => p.project_id !== id);
      this.countEl.textContent = String(this.projects.length);
      this.callbacks.onToast("Deleted saved project", "info");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to delete project";
      this.callbacks.onToast(msg, "error");
      this.callbacks.onApiError?.(err, "projects");
    } finally {
      this.busyIds.delete(id);
      this.renderList();
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
          ? `<img class="gf-proj-thumb" src="${escapeHtml(thumbSrc)}" alt="" loading="lazy" onerror="this.style.display='none'" />`
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
