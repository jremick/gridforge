# AI Agents, MCP & Interactive Chat UI Guide

GridForge ships native **AI agent integration** out of the box across every major chat, IDE, and CLI host — **Claude Desktop & Claude Code**, **OpenAI Codex**, **Google Antigravity**, **ChatGPT**, **Cursor**, and **VS Code** — without adding any external third-party AI SDK dependencies.

---

## Architecture Overview: MCP + Interactive MCP Apps (SEP-1865)

GridForge exposes a zero-dependency **Model Context Protocol (MCP)** server (supporting protocol versions `2025-06-18`, `2025-03-26`, and `2024-11-05`) over both:

- **Streamable HTTP** at `POST` / `GET` / `DELETE /mcp` (built directly into the FastAPI web server)
- **Local `stdio`** via `gridforge mcp` (in-process FastAPI ASGI or proxied to any local/remote GridForge instance via `--url`)

### Rich Interactive UI + Universal Visual Assets

1. **Interactive MCP Apps (`ui://gridforge/app.html`, SEP-1865):**
   In hosts that support the [MCP Apps extension (`ext-apps`)](https://github.com/modelcontextprotocol/ext-apps) — such as **Google Antigravity** and MCP Apps-enabled **Claude** clients — GridForge renders a self-contained single-file HTML/JS interactive workspace (`text/html;profile=mcp-app`, zero external CDN fetches, `default-src 'none'` CSP compatible) directly inside the conversation, side pane, or sidebar:
   - **Interactive UTM Footprint & MGRS Explorer:** Zoomable/pannable HTML5 canvas with multi-sheet (`A1..C3`) footprints, 1 km MGRS lines, SVG WMM2025 True/Grid/Magnetic North declination compass, and live controls to switch scale, paper, theme, atlas grid, and queue renders.
   - **Interactive Hover-Scrub RF Fresnel Chart:** Curved-Earth ($k = 4/3$) terrain clearance & 1st Fresnel zone chart with band presets (`915`/`868` MHz LoRa, `477` MHz UHF CB, `462` MHz GMRS, `146` MHz VHF, `2.4`/`5.8` GHz) and live antenna height recalculation.
   - **Terrain Viewshed & Strip Atlas Views:** Coverage overlays, waypoint route cards, and multi-sheet strip footprints.
   - **Live-Polling Printable Sheet Viewer:** Tracks queued/running PyQGIS renders automatically via `app_get_job_status`, fetches high-resolution pages on demand via `app_get_job_page` (keeping base64 out of the LLM trajectory), and offers zoom (`Fit` / `100%` / `150%` / `200%`), page navigation, and one-click artifact downloads.
2. **Crisp Inline PNG Visual Cards + Q&A Prose in *Every* MCP Host:**
   Even in hosts that do not render MCP App iframes, GridForge's tools return concise natural-language Q&A summaries in `content` text alongside compact Pillow-generated dark-tactical **PNG visual cards** (`ImageContent`, well under the 1 MiB trajectory cap) for footprints, viewsheds, route cards, 760×360 curved-Earth Fresnel charts, and rendered PDF page previews — plus clickable Studio share links and short-lived HMAC-signed artifact download URLs (`?sig=...`).
3. **Agent-Ready REST API & Discovery:**
   - `GET /llms.txt` — plain-text AI discovery index pointing to `/mcp`, `/api/capabilities`, `/openapi.json`, `/docs`, and CLI `--spec-file`.
   - `GET /api/capabilities` — machine-readable JSON catalog of supported scales, paper sizes, 4 themes, 6 DEM sources (with vertical datums), regional overlays, RF bands, output formats, installed offline packs, and caller role limits.
   - `GET /openapi.json` — complete OpenAPI 3.x schema with `CreateJobRequest` and `StripPlanRequest` JSON/multipart request bodies, `BearerAuth`, and optional `servers` URL (`GRIDFORGE_PUBLIC_URL`).

---

## Quick Setup by Client

> **Note on Rendering Jobs (`render_map`):**
> Instant analysis tools (`answer_location_briefing`, `geocode_place`, `preview_footprint`, `analyze_rf_link`, `compute_viewshed`, `measure_route`, `plan_strip_atlas`, `list_packs`, `get_capabilities`, `create_share_link`) run directly inside the web/MCP process with no background worker required. To render 300–600 DPI PDFs and offline device exports (`render_map`), connect to either:
> - Your local self-hosted stack (`docker compose -f docker-compose.ghcr.yml up -d` → `http://127.0.0.1:8080/mcp`), **or**
> - A remote hosted GridForge / GridMaps deployment (`https://<your-domain>/mcp` with a `gf_...` Bearer API key).

### 1. Claude Desktop & Claude Code

#### Option A — Local Self-Hosted Streamable HTTP (`/mcp`)

Start GridForge locally (`docker compose -f docker-compose.ghcr.yml up -d`), then register `/mcp` in **Claude Code**:

```bash
claude mcp add --transport http gridforge http://127.0.0.1:8080/mcp
```

#### Option B — Remote Hosted Instance (`https://<your-domain>/mcp` + Bearer PAT)

Mint a Personal Access Token (`gf_...`) via `POST /api/auth/api-keys` (see [Authentication & API Keys](#authentication-api-keys--signed-urls)), then connect:

```bash
claude mcp add --transport http gridforge https://<your-domain>/mcp \
  --header "Authorization: Bearer gf_<your-api-key>"
```

Or add it to `.mcp.json` (Claude Code project config):

```json
{
  "mcpServers": {
    "gridforge": {
      "type": "http",
      "url": "https://<your-domain>/mcp",
      "headers": {
        "Authorization": "Bearer gf_<your-api-key>"
      }
    }
  }
}
```

#### Option C — Local Docker `stdio` (Claude Desktop `claude_desktop_config.json`)

For **Claude Desktop** (standalone instant analysis or proxying to your running Studio/hosted server via `--url`):

```json
{
  "mcpServers": {
    "gridforge": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-e",
        "GRIDFORGE_API_KEY",
        "ghcr.io/jremick/gridforge:latest",
        "gridforge",
        "mcp",
        "--url",
        "http://host.docker.internal:8080"
      ],
      "env": {
        "GRIDFORGE_API_KEY": ""
      }
    }
  }
}
```

To target a remote hosted instance via `stdio` instead of local Docker Studio, replace `http://host.docker.internal:8080` with `https://<your-domain>` and set `"GRIDFORGE_API_KEY": "gf_<your-api-key>"`. To run completely standalone without any web server (instant briefing/RF/viewshed/footprint tools only), omit `--url` and mount a data volume (`-v gridforge-data:/data`).

---

### 2. OpenAI Codex CLI / IDE

#### MCP Configuration (`~/.codex/config.toml`)

**Streamable HTTP (recommended when using local Compose Studio or a hosted instance):**

```toml
[mcp_servers.gridforge]
url = "https://<your-domain>/mcp"
bearer_token_env_var = "GRIDFORGE_API_KEY"
```

For an unauthenticated local loopback stack (`GRIDFORGE_AUTH_MODE=none` without `GRIDFORGE_API_KEY`):

```toml
[mcp_servers.gridforge]
url = "http://127.0.0.1:8080/mcp"
```

**Local Docker `stdio`:**

```toml
[mcp_servers.gridforge]
command = "docker"
args = [
  "run", "-i", "--rm",
  "ghcr.io/jremick/gridforge:latest",
  "gridforge", "mcp",
  "--url", "http://host.docker.internal:8080"
]
```

#### Install the Agent Skill + Shell `--spec-file` Workflow

Copy [`docs/agent-skill/SKILL.md`](agent-skill/SKILL.md) into your Codex skills directory so Codex knows regional DEMs, overlays, themes, and CLI `--spec-file` JSON payloads automatically:

```bash
mkdir -p ~/.codex/skills/gridforge
cp docs/agent-skill/SKILL.md ~/.codex/skills/gridforge/SKILL.md
```

---

### 3. Google Antigravity (Inline MCP Apps + Side Pane + Sidebar)

GridForge includes a ready-to-install Antigravity plugin template in [`plugins/antigravity/`](../plugins/antigravity/README.md). Installing it enables:

- **Inline Interactive MCP Apps** inside chat turns (footprint canvas, WMM2025 compass, hover-scrub RF Fresnel chart, live-polling multi-page PDF sheet viewer).
- **Side Pane (`aux_pane`) entrypoint — `GridForge Studio`:** Open from the side pane `+` menu or let the agent drive it live via `open_gridforge_studio`.
- **Sidebar (`global`) entrypoint — `GridForge Maps`:** Full-page map & RF workspace in the left sidebar.

Install in 30 seconds:

```bash
cp -r plugins/antigravity ~/.gemini/config/plugins/gridforge

# Optional: bundle the end-user cartography skill inside the same plugin
mkdir -p ~/.gemini/config/plugins/gridforge/skills/gridforge
cp docs/agent-skill/SKILL.md ~/.gemini/config/plugins/gridforge/skills/gridforge/SKILL.md
```

See [`plugins/antigravity/README.md`](../plugins/antigravity/README.md) for switching between standalone local Docker `stdio`, a local Compose Studio stack, or a remote hosted instance (`https://<your-domain>/mcp` with `Authorization: Bearer gf_...`).

---

### 4. ChatGPT (Custom GPT Actions & Remote MCP)

#### Option A — Custom GPT Actions (OpenAPI Import)

1. Deploy GridForge with HTTPS and set `GRIDFORGE_PUBLIC_URL=https://<your-domain>`, `GRIDFORGE_ALLOWED_HOSTS=<your-domain>`, and `GRIDFORGE_ENABLE_DOCS=true` (hosted mode disables `/openapi.json` and `/docs` by default — see [`deploy/hosted-mode.md`](../deploy/hosted-mode.md)).
2. Mint a Bearer API key (`gf_...`) via `POST /api/auth/api-keys` (or set `GRIDFORGE_API_KEY` on a single-tenant instance).
3. In ChatGPT **Create a GPT → Configure → Actions → Create new action**:
   - **Schema:** Click **Import from URL** and enter `https://<your-domain>/openapi.json`.
   - **Authentication:** Choose **API Key** → **Auth Type: Bearer** → paste your `gf_...` API key.
4. Recommended **Custom GPT Instructions**:

```text
You are a topographic mapping, MGRS navigation, and RF line-of-sight assistant powered by GridForge.
- Call GET /api/capabilities first if you need exact scales, themes, DEM sources, or regional overlays.
- For quick location, MGRS, declination, or footprint questions, call GET /api/geocode and POST /api/footprint.
- For radio / LoRa / Meshtastic link checks, call POST /api/rf-link-profile and report CLEAR/MARGINAL/OBSTRUCTED, 1st Fresnel clearance, FSPL dB, and True/Grid/Magnetic bearings + NATO mils.
- For printable PDFs or ATAK/Garmin/OsmAnd exports, call POST /api/jobs?wait=true&timeout_s=120 with JSON body matching CreateJobRequest.
- When a job succeeds, always present the clickable `signed_download_url` links from `artifacts` (noting they expire in 1 hour), optionally inspect pages via GET /api/jobs/{job_id}/pages, and create a read-only Studio link via POST /api/shares when helpful.
```

Because `POST /api/jobs?wait=true` polls internally until the worker finishes (returning `200 OK` when terminal, or `202 Accepted` if still running) and returns `signed_download_url` (`?sig=...`) on every artifact, ChatGPT users can click directly to download PDFs, ATAK `.mbtiles`, Garmin `.kmz`, GeoPackages, OsmAnd `.sqlitedb`, or Field Bundle `.zip` archives without sharing cookies or Bearer tokens.

#### Option B — ChatGPT Remote MCP Connector

For ChatGPT plans supporting remote MCP servers, register `https://<your-domain>/mcp` over Streamable HTTP with Bearer authentication (`Authorization: Bearer gf_<your-api-key>`).

---

### 5. Cursor, VS Code & Generic MCP Clients

**Streamable HTTP (`.cursor/mcp.json` or `.vscode/mcp.json`):**

```json
{
  "mcpServers": {
    "gridforge": {
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

With Bearer authentication against a hosted instance:

```json
{
  "mcpServers": {
    "gridforge": {
      "url": "https://<your-domain>/mcp",
      "headers": {
        "Authorization": "Bearer gf_<your-api-key>"
      }
    }
  }
}
```

**Local Docker `stdio` (`.cursor/mcp.json`):**

```json
{
  "mcpServers": {
    "gridforge": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "ghcr.io/jremick/gridforge:latest",
        "gridforge",
        "mcp",
        "--url",
        "http://host.docker.internal:8080"
      ]
    }
  }
}
```

---

## MCP Server Reference

### Model-Visible Tools (14)

| Tool | UI / Visual Output | Description & Key Behavior |
| :--- | :--- | :--- |
| `open_gridforge_studio` | Interactive Studio (`aux_pane`: **GridForge Studio**, `global`: **GridForge Maps**) | Opens the interactive Studio launcher with regional presets, server capabilities, and installed offline packs. All arguments optional (`{}` works for sidebar/pane entrypoints). |
| `answer_location_briefing` | Interactive `briefing` + Footprint PNG card | Accepts a place name, `"lat,lon"`, or MGRS string (plus optional `question`, `scale`, `paper`, `orientation`, `color_mode`, `grid_cols`, `grid_rows`). Geocodes if needed, computes UTM footprint & WMM2025 declination, recommends regional LiDAR DEM/overlays & matching packs, and returns prose + PNG + GeoJSON. |
| `geocode_place` | Interactive `geocode` result picker | Searches places via `GET /api/geocode`; clicking any result in the MCP App jumps straight to a full location briefing. |
| `preview_footprint` | Interactive `briefing` + Footprint PNG card | Computes true-scale local UTM footprint (`1×1`–`3×3`), 1 km MGRS grid GeoJSON, and WMM2025 declination via `POST /api/footprint`. |
| `analyze_rf_link` | Interactive `rf_link` Fresnel chart + 760×360 PNG | Models curved-Earth ($k = 4/3$) LOS, 1st Fresnel & 60% clearance, FSPL (dB), and True/Grid/Mag bearings + NATO mils via `POST /api/rf-link-profile`. Supports live band/antenna-height recalculation in the widget. |
| `compute_viewshed` | Interactive `viewshed` map + Viewshed PNG card | Computes GDAL terrain-masked RF/LOS coverage polygon, visible area (km²), observer elevation, and dashed range ring via `POST /api/viewshed-preview`. |
| `measure_route` | Interactive `route` map & table + Route PNG card | Computes waypoint leg distances, True/Grid/Mag azimuths, NATO mils, and DEM slope/elevation metrics via `POST /api/route-metrics` or `POST /api/ruler-metrics`. |
| `plan_strip_atlas` | Interactive `route` strip-sheet planner | Fits minimum-count true-scale UTM strip sheets (`S1..Sn`) along a GeoJSON track via `POST /api/strip-plan`. |
| `render_map` | Interactive `render` sheet viewer + Page 1 PNG | Submits `POST /api/jobs` (`wait=true` default, configurable `timeout_s`) supporting scales `1:10k`–`1:50k`, 4 themes, 300–600 DPI, multi-sheet grids, overlays, waypoints, viewsheds, RF links, tactical graphics, supplements, and all offline device exports. Returns clickable signed download URLs and Studio share link. |
| `get_job_status` | Interactive `render` viewer + optional Page 1 PNG | Fetches job status, stage, progress, artifacts (with `signed_download_url`), and page list via `GET /api/jobs/{job_id}`. |
| `get_job_page_preview` | `ImageContent` (PNG) | Fetches any rasterized PDF page (`/api/jobs/{job_id}/pages/{page}.png?width=...`, 240–2400 px) so the model can visually inspect page 2+ (RF Link Cards, Route Cards, Atlas Index Covers, Calibration Sheets). |
| `create_share_link` | Text + Share URL | Creates a read-only server-backed Studio snapshot link (`/?share=<token>`) via `POST /api/shares`. |
| `list_packs` | Interactive `packs` browser | Lists installed air-gapped offline packs (`GET /api/packs`) and the 29 curated regional pack presets (`GET /api/packs/regions`). |
| `get_capabilities` | Structured JSON + summary | Returns full server capabilities, scales, themes, DEM sources, overlays, RF bands, output formats, and caller quotas via `GET /api/capabilities`. |

### App-Only Tools (3)

Declared with `_meta.ui.visibility = ["app"]` so MCP hosts hide them from the LLM while permitting the interactive MCP App iframe to call them via `app.callServerTool()`:

| Tool | Purpose |
| :--- | :--- |
| `app_get_job_status` | Polls live render stage & progress (`queued` / `running` / `succeeded` / `failed`) every 2 s inside the Printable Sheet Viewer. |
| `app_get_job_page` | Streams high-resolution page PNGs (`width=1100` default) to the sheet viewer without inserting large base64 payloads into the LLM conversation trajectory. |
| `app_get_capabilities` | Populates interactive widget dropdowns (scales, themes, overlays, installed packs). |

### MCP Resources (3)

| URI | MIME Type | Description |
| :--- | :--- | :--- |
| `ui://gridforge/app.html` | `text/html;profile=mcp-app` | Single-file SEP-1865 interactive MCP App bundle (`_meta.ui.prefersBorder = false`). Override on disk via `GRIDFORGE_MCP_APP_HTML` for live UI iteration. |
| `gridforge://capabilities` | `application/json` | Live server capability catalog and regional open-data metadata. |
| `gridforge://guide` | `text/markdown` | Compact agent reference for scales, themes, regional DEMs, overlays, and workflows. |

### MCP Prompts (4)

| Prompt | Description |
| :--- | :--- |
| `plan_backcountry_map` | Guided workflow to pick scale, paper, theme, regional DEM/overlays, field supplements, and render a print sheet. |
| `analyze_lora_link` | Guided point-to-point 915/868 MHz LoRa / Meshtastic Fresnel clearance & viewshed analysis with optional Link Card PDF. |
| `navigation_briefing` | Complete MGRS, UTM zone, WMM2025 declination/G-M angle, and regional open-data briefing for any coordinate or place. |
| `plan_strip_atlas` | Auto-fitted strip atlas planning along a trail or track with Atlas Index Cover, Elevation Profile, and Fold Marks. |

---

## Authentication, API Keys & Signed URLs

### Hosted Multi-Tenant Mode (`GRIDFORGE_AUTH_MODE=hosted`)

Authenticated **Friend** and **Admin** accounts can mint scoped Bearer Personal Access Tokens (`gf_<token>`):

```bash
# 1. Mint a new API key (using an existing authenticated session cookie or API key)
curl -sS -X POST "https://<your-domain>/api/auth/api-keys" \
  -H "Content-Type: application/json" \
  -H "Origin: https://<your-domain>" \
  -b cookies.txt \
  -d '{"name": "Claude Desktop", "expires_days": 90}'

# 2. List active API keys (shows prefix & last_used_at; never plaintext)
curl -sS "https://<your-domain>/api/auth/api-keys" \
  -H "Authorization: Bearer gf_<your-api-key>"

# 3. Revoke a key immediately
curl -sS -X DELETE "https://<your-domain>/api/auth/api-keys/<key_id>" \
  -H "Authorization: Bearer gf_<your-api-key>"
```

Key security properties:
- **Returned once, hashed at rest:** The plaintext `gf_...` token is shown only in the `201 Created` response and stored as a SHA-256 hash in SQLite (`api_keys` table).
- **Header support:** Pass either `Authorization: Bearer gf_...` or `X-API-Key: gf_...` on both `/api/*` and `/mcp`.
- **Automatic Turnstile bypass with full quota parity:** Valid API keys authenticate directly as the owning `friend` or `admin` user — bypassing guest-only Cloudflare Turnstile checks while enforcing all per-role daily render caps, hourly preview limits, DPI/grid caps, and tenant isolation.
- **Fail closed:** Invalid, revoked, expired, or disabled-user API keys always return `401 Unauthorized`.

### Single-Tenant Self-Hosted Mode (`GRIDFORGE_AUTH_MODE=none`)

- When `GRIDFORGE_API_KEY` is **unset** (default): zero-login local loopback (`127.0.0.1:8080`) works out of the box with no auth header required.
- When `GRIDFORGE_API_KEY` is **set**: mutating `/api/*` calls and `/mcp` require a matching `Authorization: Bearer <key>` or `X-API-Key: <key>` (constant-time compare), returning `401 Unauthorized` otherwise.

### Short-Lived HMAC-Signed Artifact & Preview URLs (`?sig=...`)

Every job artifact returned by `POST /api/jobs`, `GET /api/jobs`, and `GET /api/jobs/{job_id}` includes:
- `download_url`: standard authenticated endpoint (`/api/jobs/{job_id}/artifacts/{filename}`)
- `signed_download_url`: short-lived HMAC-signed URL (`/api/jobs/{job_id}/artifacts/{filename}?sig=...`)

Similarly, `GET /api/jobs/{job_id}/pages` returns per-page `preview_url` and `signed_preview_url` (`/api/jobs/{job_id}/pages/{page}.png?sig=...`).

Signed tokens are scoped strictly to `(job_id, resource)` and expire after `GRIDFORGE_SIGNED_URL_TTL_SECONDS` (default `3600` s / 1 hour). Agents can safely hand clickable download and preview links directly to users in chat without leaking cookies or API keys.

---

## CLI `--spec-file` & `--json` Examples (Shell-Based Coding Agents)

Shell-based coding agents (Claude Code, Codex CLI, CI pipelines) can inspect telemetry or render complex multi-layer maps cleanly using structured JSON instead of long comma-delimited flag strings:

### 1. Instant JSON Navigation Inspection (`gridforge inspect --json`)

```bash
docker run --rm ghcr.io/jremick/gridforge:latest \
  gridforge inspect --center "-36.456,148.263" --scale 25000 --paper a4 --json
```

### 2. Declarative Rendering with `--spec-file` (File or Stdin `-`)

Write a JSON spec matching `CreateJobRequest` / `SheetSpec` (for example, `out/yosemite-spec.json`):

```json
{
  "center": "37.745,-119.593",
  "title": "YOSEMITE VALLEY — ALPINE CA",
  "scale": 24000,
  "paper": "letter",
  "orientation": "portrait",
  "color_mode": "alpine",
  "units": "imperial",
  "dpi": 300,
  "dem_source": "usgs3dep",
  "us_overlays": ["us-trails", "us-public-lands"],
  "carto": {
    "show_slope_shading": true,
    "contour_density": "normal",
    "hillshade_strength": "normal"
  },
  "waypoints": [
    {"label": "Happy Isles", "lat": 37.7318, "lon": -119.5586, "category": "waypoint"},
    {"label": "Vernal Fall", "lat": 37.7275, "lon": -119.5439, "category": "water"}
  ],
  "include_route_card": true,
  "include_calibration_page": true
}
```

Render from file or stream directly over `stdin` (`--spec-file -`) with machine-readable `--json` output:

```bash
# From a JSON file
docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --spec-file /out/yosemite-spec.json \
    --output /out/yosemite_alpine.pdf --json

# Or piped over stdin
cat out/yosemite-spec.json | docker run -i --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --spec-file - --output /out/yosemite_alpine.pdf --json
```

### 3. Launching the MCP `stdio` Server Manually

```bash
# Local in-process mode (instant analysis tools)
docker run -i --rm -v "$(pwd)/packs:/data/packs:ro" ghcr.io/jremick/gridforge:latest \
  gridforge mcp --data-dir /tmp/gridforge-jobs --packs-dir /data/packs

# Proxy to a running local Studio or remote hosted deployment
docker run -i --rm -e GRIDFORGE_API_KEY="gf_<your-api-key>" ghcr.io/jremick/gridforge:latest \
  gridforge mcp --url "https://<your-domain>" --public-url "https://<your-domain>"
```

See [`deploy/hosted-mode.md`](../deploy/hosted-mode.md) for full hosted environment variable documentation.
