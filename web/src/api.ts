import type {
  BasemapInfoResponse,
  CreateJobPayload,
  DemSourceKind,
  FootprintRequest,
  FootprintResponse,
  GeocodeResponse,
  HealthResponse,
  JobRecord,
  Orientation,
  PackBuildRequest,
  PackBuildTask,
  PackRegionGroup,
  PackRegionPreset,
  PacksResponse,
  PaperSize,
  RFLinkProfileResponse,
  RFLinkSpec,
  RouteMetricsResponse,
  RulerMetricsResponse,
  SavedProject,
  StripPlanResponse,
  Units,
  ViewshedObserver,
  ViewshedPreviewResponse,
  WaypointInput,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseErrorDetail(res: Response): Promise<string> {
  try {
    const contentType = res.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const body = (await res.json()) as {
        detail?: unknown;
        message?: unknown;
      };
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) {
        return body.detail
          .map((item: unknown) => {
            if (item && typeof item === "object" && "msg" in item) {
              const loc =
                "loc" in item && Array.isArray((item as { loc?: unknown[] }).loc)
                  ? `${((item as { loc: unknown[] }).loc).join(".")}: `
                  : "";
              return `${loc}${String((item as { msg: unknown }).msg)}`;
            }
            return String(item);
          })
          .join("; ");
      }
      if (typeof body.message === "string") return body.message;
    }
    const text = await res.text();
    if (text.trim()) return text.slice(0, 220);
  } catch {
    // Ignore parse fallback failure
  }
  return `HTTP ${res.status} ${res.statusText}`;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export function fetchHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/api/health");
}

export function fetchBasemapInfo(
  packId?: string | null
): Promise<BasemapInfoResponse> {
  const qs = packId ? `?pack_id=${encodeURIComponent(packId)}` : "";
  return requestJson<BasemapInfoResponse>(`/api/basemap/info${qs}`);
}

export function fetchPacks(): Promise<PacksResponse> {
  return requestJson<PacksResponse>("/api/packs");
}

interface RawPackRegion {
  region_id?: string;
  id?: string;
  name: string;
  group?: string;
  kind?: string;
  bbox_wgs84: PackRegionPreset["bbox_wgs84"];
  utm_zones?: string[];
  description?: string;
  estimated_size_hint?: string;
}

function normalizeRegionGroup(raw: RawPackRegion): PackRegionGroup {
  if (
    raw.group === "au_tactical" ||
    raw.group === "au_state" ||
    raw.group === "nz" ||
    raw.group === "na"
  ) {
    return raw.group;
  }
  return raw.kind === "state" ? "au_state" : "au_tactical";
}

export async function fetchPackRegions(): Promise<PackRegionPreset[]> {
  const data = await requestJson<
    RawPackRegion[] | { regions: RawPackRegion[] }
  >("/api/packs/regions");
  const rawList = Array.isArray(data) ? data : data.regions;
  return rawList.map((r) => ({
    region_id: r.region_id ?? r.id ?? r.name,
    name: r.name,
    group: normalizeRegionGroup(r),
    kind: r.kind === "state" ? "state" : "tactical",
    bbox_wgs84: r.bbox_wgs84,
    utm_zones: r.utm_zones ?? [],
    description: r.description,
    estimated_size_hint: r.estimated_size_hint,
  }));
}

export function startPackBuild(
  req: PackBuildRequest
): Promise<PackBuildTask> {
  return requestJson<PackBuildTask>("/api/packs/build", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export async function fetchPackTasks(): Promise<PackBuildTask[]> {
  const data = await requestJson<PackBuildTask[] | { tasks: PackBuildTask[] }>(
    "/api/packs/tasks"
  );
  return Array.isArray(data) ? data : data.tasks;
}

export async function deletePack(packId: string): Promise<void> {
  const res = await fetch(`/api/packs/${encodeURIComponent(packId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }
}

export function fetchGeocode(
  query: string,
  limit = 5,
  signal?: AbortSignal
): Promise<GeocodeResponse> {
  const params = new URLSearchParams({
    q: query,
    limit: String(limit),
  });
  return requestJson<GeocodeResponse>(`/api/geocode?${params.toString()}`, {
    signal,
  });
}

export function fetchFootprint(
  req: FootprintRequest,
  signal?: AbortSignal
): Promise<FootprintResponse> {
  return requestJson<FootprintResponse>("/api/footprint", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
}

export function fetchViewshedPreview(
  observer: ViewshedObserver,
  demSource: DemSourceKind = "auto",
  packId?: string | null,
  signal?: AbortSignal
): Promise<ViewshedPreviewResponse> {
  return requestJson<ViewshedPreviewResponse>("/api/viewshed-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      observer,
      dem_source: demSource,
      pack_id: packId || null,
    }),
    signal,
  });
}

export function fetchRFLinkProfile(
  spec: RFLinkSpec,
  demSource: DemSourceKind = "auto",
  packId?: string | null,
  signal?: AbortSignal
): Promise<RFLinkProfileResponse> {
  return requestJson<RFLinkProfileResponse>("/api/rf-link-profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...spec,
      dem_source: demSource,
      pack_id: packId || null,
    }),
    signal,
  });
}

export function fetchRulerMetrics(
  points: { lat: number; lon: number }[],
  demSource?: DemSourceKind,
  packId?: string | null,
  signal?: AbortSignal
): Promise<RulerMetricsResponse> {
  return requestJson<RulerMetricsResponse>("/api/ruler-metrics", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      points,
      dem_source: demSource ?? "auto",
      pack_id: packId || null,
    }),
    signal,
  });
}

export function fetchStripPlan(
  routeFile: File,
  params: {
    scale: number;
    paper: PaperSize;
    orientation: Orientation;
    units: Units;
    overlap_pct?: number;
  },
  signal?: AbortSignal
): Promise<StripPlanResponse> {
  const form = new FormData();
  form.append("route_file", routeFile, routeFile.name);
  form.append("scale", String(params.scale));
  form.append("paper", params.paper);
  form.append("orientation", params.orientation);
  form.append("units", params.units);
  if (typeof params.overlap_pct === "number") {
    form.append("overlap_pct", String(params.overlap_pct));
  }
  return requestJson<StripPlanResponse>("/api/strip-plan", {
    method: "POST",
    body: form,
    signal,
  });
}

export function fetchRouteMetrics(
  waypoints: WaypointInput[],
  units: Units,
  dateIso?: string | null,
  signal?: AbortSignal
): Promise<RouteMetricsResponse> {
  return requestJson<RouteMetricsResponse>("/api/route-metrics", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      waypoints,
      units,
      date_iso: dateIso ?? null,
    }),
    signal,
  });
}

interface RawSavedProject {
  id?: string;
  project_id?: string;
  name: string;
  description?: string | null;
  notes?: string | null;
  created_at: string;
  updated_at: string;
  last_job_id?: string | null;
  thumbnail_url?: string | null;
  state: Record<string, unknown>;
}

function normalizeSavedProject(raw: RawSavedProject): SavedProject {
  const id = raw.project_id ?? raw.id ?? "";
  const desc = raw.description ?? raw.notes ?? null;
  return {
    project_id: id,
    name: raw.name,
    description: desc,
    notes: desc,
    created_at: raw.created_at,
    updated_at: raw.updated_at,
    last_job_id: raw.last_job_id ?? null,
    thumbnail_url: raw.thumbnail_url ?? null,
    state: raw.state ?? {},
  };
}

export async function fetchProjects(): Promise<SavedProject[]> {
  const data = await requestJson<
    RawSavedProject[] | { projects: RawSavedProject[] }
  >("/api/projects");
  const list = Array.isArray(data) ? data : data.projects;
  return list.map(normalizeSavedProject);
}

export async function createProject(req: {
  name: string;
  description?: string | null;
  notes?: string | null;
  state: Record<string, unknown>;
  last_job_id?: string | null;
}): Promise<SavedProject> {
  const description = (req.description ?? req.notes ?? "").trim();
  const raw = await requestJson<RawSavedProject>("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: req.name,
      description,
      notes: description,
      state: req.state,
      last_job_id: req.last_job_id ?? null,
    }),
  });
  return normalizeSavedProject(raw);
}

export async function deleteProject(projectId: string): Promise<void> {
  const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }
}

export async function fetchJobs(): Promise<JobRecord[]> {
  const data = await requestJson<JobRecord[] | { jobs: JobRecord[] }>(
    "/api/jobs"
  );
  return Array.isArray(data) ? data : data.jobs;
}

export function submitJob(
  payload: CreateJobPayload,
  overlayFile?: File | null
): Promise<JobRecord> {
  const form = new FormData();
  form.append("payload", JSON.stringify(payload));
  if (overlayFile) {
    form.append("overlay_file", overlayFile, overlayFile.name);
  }
  return requestJson<JobRecord>("/api/jobs", {
    method: "POST",
    body: form,
  });
}

export function cancelJob(jobId: string): Promise<JobRecord> {
  return requestJson<JobRecord>(
    `/api/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" }
  );
}

export async function deleteJob(jobId: string): Promise<void> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }
}

export function artifactDownloadUrl(
  jobId: string,
  filename: string,
  explicitUrl?: string
): string {
  if (explicitUrl && explicitUrl.startsWith("/")) {
    return explicitUrl;
  }
  return `/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(filename)}`;
}

export function artifactPreviewUrl(
  jobId: string,
  filename: string,
  explicitUrl?: string
): string {
  const base = artifactDownloadUrl(jobId, filename, explicitUrl);
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}inline=1`;
}

export function jobThumbnailUrl(jobId: string): string {
  return `/api/jobs/${encodeURIComponent(jobId)}/thumbnail.png`;
}
