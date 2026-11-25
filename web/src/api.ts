import type {
  CreateJobPayload,
  FootprintRequest,
  FootprintResponse,
  GeocodeResponse,
  HealthResponse,
  JobRecord,
  PacksResponse,
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

export function fetchPacks(): Promise<PacksResponse> {
  return requestJson<PacksResponse>("/api/packs");
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
