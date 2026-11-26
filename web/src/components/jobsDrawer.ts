import {
  artifactDownloadUrl,
  artifactPreviewUrl,
  jobThumbnailUrl,
} from "../api";
import {
  escapeHtml,
  formatArtifactLabel,
  formatBytes,
  formatDuration,
  formatStage,
} from "../presets";
import type { JobRecord } from "../types";

export interface JobsDrawerCallbacks {
  onClose: () => void;
  onRefresh: () => void;
  onCancelJob: (jobId: string) => void;
  onDeleteJob: (jobId: string) => void;
}

function computeElapsedSeconds(job: JobRecord): number | null {
  if (typeof job.elapsed_seconds === "number") {
    return job.elapsed_seconds;
  }
  if (job.status === "running" && job.started_at) {
    const startMs = Date.parse(job.started_at);
    if (!Number.isNaN(startMs)) {
      return Math.max(0, (Date.now() - startMs) / 1000);
    }
  }
  return null;
}

function formatShortTime(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Date(ms).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function isPdfArtifact(kind: string, filename: string): boolean {
  return (
    kind.toLowerCase().includes("pdf") ||
    filename.toLowerCase().endsWith(".pdf")
  );
}

export class JobsDrawer {
  private readonly root: HTMLElement;
  private readonly bodyEl: HTMLElement;
  private readonly countEl: HTMLElement;
  private readonly callbacks: JobsDrawerCallbacks;
  private jobs: JobRecord[] = [];
  private busyJobIds = new Set<string>();

  constructor(container: HTMLElement, callbacks: JobsDrawerCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "gf-drawer";
    this.root.setAttribute("aria-label", "Render Job Queue");
    this.root.innerHTML = `
      <div class="gf-drawer-header">
        <div class="gf-drawer-title">
          <span>Render Queue</span>
          <span class="gf-section-pill" data-role="drawer-count">0</span>
        </div>
        <div class="gf-drawer-actions">
          <button type="button" class="gf-icon-btn" data-role="drawer-refresh" title="Refresh jobs">↻</button>
          <button type="button" class="gf-icon-btn" data-role="drawer-close" title="Close queue drawer">×</button>
        </div>
      </div>
      <div class="gf-drawer-body" data-role="drawer-body"></div>
    `;

    this.bodyEl = this.q("[data-role='drawer-body']");
    this.countEl = this.q("[data-role='drawer-count']");

    this.q("[data-role='drawer-close']").addEventListener("click", () => {
      this.callbacks.onClose();
    });
    this.q("[data-role='drawer-refresh']").addEventListener("click", () => {
      this.callbacks.onRefresh();
    });

    this.bodyEl.addEventListener("click", (e) => {
      const target = e.target as HTMLElement | null;
      const btn = target?.closest<HTMLButtonElement>("button[data-action]");
      if (!btn) return;
      const action = btn.dataset.action;
      const jobId = btn.dataset.jobId;
      if (!jobId) return;

      if (action === "cancel") {
        this.busyJobIds.add(jobId);
        this.renderJobs();
        this.callbacks.onCancelJob(jobId);
      } else if (action === "delete") {
        this.busyJobIds.add(jobId);
        this.renderJobs();
        this.callbacks.onDeleteJob(jobId);
      }
    });

    this.renderJobs();
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Drawer element not found: ${selector}`);
    return el;
  }

  setJobs(jobs: JobRecord[]): void {
    this.jobs = jobs;
    this.busyJobIds.clear();
    this.countEl.textContent = String(jobs.length);
    this.renderJobs();
  }

  clearBusy(jobId: string): void {
    this.busyJobIds.delete(jobId);
    this.renderJobs();
  }

  tickElapsed(): void {
    this.bodyEl
      .querySelectorAll<HTMLElement>("[data-elapsed-start]")
      .forEach((el) => {
        const started = el.dataset.elapsedStart;
        if (!started) return;
        const startMs = Date.parse(started);
        if (!Number.isNaN(startMs)) {
          const secs = Math.max(0, (Date.now() - startMs) / 1000);
          el.textContent = `Elapsed ${formatDuration(secs)}`;
        }
      });
  }

  private renderJobs(): void {
    if (this.jobs.length === 0) {
      this.bodyEl.innerHTML = `
        <div class="gf-empty-state">
          <div class="gf-empty-title">No map sheets queued</div>
          <div>Position the amber sheet frame on the map, choose print/digital outputs, and click <strong>Generate Map Sheet</strong>.</div>
        </div>
      `;
      return;
    }

    this.bodyEl.innerHTML = this.jobs
      .map((job) => this.renderJobCard(job))
      .join("");
  }

  private renderJobCard(job: JobRecord): string {
    const displayTitle =
      job.title?.trim() || job.center_mgrs || job.center || "Untitled Sheet";
    const scaleStr = `1:${job.scale.toLocaleString()}`;
    const paperStr = `${job.paper.toUpperCase()} ${job.orientation === "landscape" ? "Landscape" : "Portrait"}`;
    const modeStr =
      job.color_mode === "bw"
        ? "B&W"
        : job.color_mode === "night-red"
          ? "Night Red"
          : job.color_mode === "alpine"
            ? "Alpine"
            : "Color";
    const cols = job.grid_cols ?? 1;
    const rows = job.grid_rows ?? 1;
    const atlasStr = cols * rows > 1 ? ` · ${cols}×${rows} Atlas` : "";
    const pct = Math.max(0, Math.min(100, Math.round(job.progress_pct ?? 0)));
    const elapsed = computeElapsedSeconds(job);
    const isActive = job.status === "queued" || job.status === "running";
    const isBusy = this.busyJobIds.has(job.job_id);

    const progressClass =
      job.status === "succeeded"
        ? "succeeded"
        : job.status === "failed"
          ? "failed"
          : "";

    const metricsHtmlParts: string[] = [
      `<span>Created ${escapeHtml(formatShortTime(job.created_at))}</span>`,
    ];
    if (elapsed !== null) {
      const liveAttr =
        job.status === "running" && job.started_at
          ? ` data-elapsed-start="${escapeHtml(job.started_at)}"`
          : "";
      metricsHtmlParts.push(
        `<span${liveAttr}>Elapsed ${escapeHtml(formatDuration(elapsed))}</span>`
      );
    }
    if (typeof job.peak_rss_mb === "number" && job.peak_rss_mb > 0) {
      metricsHtmlParts.push(
        `<span>Peak RSS ${escapeHtml(String(Math.round(job.peak_rss_mb)))} MB</span>`
      );
    }

    const artifactsHtml =
      job.artifacts.length > 0
        ? `
          <div class="gf-artifact-list">
            ${job.artifacts
              .map((art) => {
                const downloadHref = artifactDownloadUrl(
                  job.job_id,
                  art.filename,
                  art.download_url
                );
                const label = formatArtifactLabel(art.kind, art.filename);
                if (isPdfArtifact(art.kind, art.filename)) {
                  const previewHref = artifactPreviewUrl(
                    job.job_id,
                    art.filename,
                    art.download_url
                  );
                  return `
                    <div class="gf-artifact-group">
                      <a
                        class="gf-artifact-preview-btn"
                        href="${escapeHtml(previewHref)}"
                        target="_blank"
                        rel="noopener"
                        title="Open ${escapeHtml(art.filename)} in new tab"
                      >
                        <span>↗ Preview</span>
                      </a>
                      <a
                        class="gf-artifact-btn"
                        href="${escapeHtml(downloadHref)}"
                        download="${escapeHtml(art.filename)}"
                        title="Download ${escapeHtml(art.filename)}"
                      >
                        <span>↓ ${escapeHtml(label)}</span>
                        <span class="gf-artifact-size">${escapeHtml(formatBytes(art.size_bytes))}</span>
                      </a>
                    </div>
                  `;
                }
                return `
                  <a
                    class="gf-artifact-btn"
                    href="${escapeHtml(downloadHref)}"
                    download="${escapeHtml(art.filename)}"
                    target="_blank"
                    rel="noopener"
                  >
                    <span>↓ ${escapeHtml(label)}</span>
                    <span class="gf-artifact-size">${escapeHtml(formatBytes(art.size_bytes))}</span>
                  </a>
                `;
              })
              .join("")}
          </div>
        `
        : "";

    const errorHtml = job.error
      ? `<div class="gf-job-error">${escapeHtml(job.error)}</div>`
      : "";

    const actionButtons = isActive
      ? `<button type="button" class="gf-mini-btn danger" data-action="cancel" data-job-id="${escapeHtml(job.job_id)}" ${isBusy ? "disabled" : ""}>${isBusy ? "Cancelling…" : "Cancel"}</button>`
      : `<button type="button" class="gf-mini-btn danger" data-action="delete" data-job-id="${escapeHtml(job.job_id)}" ${isBusy ? "disabled" : ""}>${isBusy ? "Deleting…" : "Delete"}</button>`;

    const thumbHtml =
      job.status === "succeeded"
        ? `<img class="gf-job-thumb" src="${escapeHtml(jobThumbnailUrl(job.job_id))}" alt="" loading="lazy" onerror="this.style.display='none'" />`
        : "";

    return `
      <article class="gf-job-card status-${escapeHtml(job.status)}">
        <div class="gf-job-head">
          <div class="gf-job-head-left">
            ${thumbHtml}
            <div>
              <div class="gf-job-title">${escapeHtml(displayTitle)}</div>
              <div class="gf-job-sub">${escapeHtml(job.center_mgrs ?? job.center)} · ${escapeHtml(scaleStr)} · ${escapeHtml(paperStr)} · ${escapeHtml(modeStr)}${escapeHtml(atlasStr)}</div>
            </div>
          </div>
          <span class="gf-badge ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>
        </div>

        <div class="gf-progress-wrap">
          <div class="gf-progress-meta">
            <span>${escapeHtml(formatStage(job.stage || job.status))}</span>
            <span class="mono">${pct}%</span>
          </div>
          <div class="gf-progress-track">
            <div class="gf-progress-fill ${progressClass}" style="width: ${pct}%"></div>
          </div>
        </div>

        <div class="gf-job-metrics">
          ${metricsHtmlParts.join("")}
        </div>

        ${errorHtml}
        ${artifactsHtml}

        <div class="gf-job-footer">
          <span class="gf-job-id">#${escapeHtml(job.job_id.slice(0, 8))}</span>
          <div class="gf-job-btn-row">
            ${actionButtons}
          </div>
        </div>
      </article>
    `;
  }
}
