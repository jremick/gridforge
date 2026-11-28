# Changelog

All notable changes to **GridForge** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.0] - 2026-11-28

Major **GridForge Studio** UI/UX and information-architecture redesign introducing a warm-paper **Daylight** theme alongside Dark mode, an upward-expanding **Outputs** drawer that reclaims sidebar scrolling space, auto-matched regional overlay tabs, self-hosted `IBM Plex` typography, unified inline SVG iconography, responsive mobile ergonomics, interactive MCP App theme parity, and a production **MapLibre GL JS v6** Web Worker bundling fix.

### Added

- **Warm-Paper Daylight Studio Theme & Persistence (`web/src/themePrefs.ts`, `web/src/styles.css`, `web/src/components/{telemetry,rfChart}.ts`, `web/src/map.ts`, `web/mcp-app/styles.css`):**
  - Added a Sun/Moon theme switcher in the top telemetry bar toggling between Dark Studio (`data-theme="dark"`) and warm archival-paper Daylight Studio (`data-theme="light"`, persisted in `localStorage`), with WCAG AA contrast tokens, explicit `color-scheme` form controls, theme-reactive MapLibre background and RF Fresnel chart variables, and matching interactive MCP App light/dark tokens.
- **Upward-Expanding Outputs Drawer & Footer GitHub Link (`web/src/components/sidebar.ts`, `web/src/styles.css`):**
  - Replaced the tall always-open sticky output wall in the sidebar footer with a compact live summary trigger (`Outputs · Print PDF 300 DPI`) that expands an anchored **Print & Digital Outputs** drawer upward on demand, shrinking the footer from `~45%` to `~15%` of `1024×768` laptop viewport height.
  - Added an `Open-source · GPL-3.0` + GitHub repository link (`https://github.com/jremick/gridforge`, `rel="noopener noreferrer"`) to the sidebar footer.
- **Self-Hosted Bundled Typography & Unified Icon System (`web/package.json`, `web/src/icons.ts`, `web/src/main.ts`):**
  - Bundled `@fontsource/ibm-plex-sans` and `@fontsource/ibm-plex-mono` Latin `.woff2`/`.woff` weights directly into Vite's static `/assets/` output for crisp offline/air-gapped typography with zero external CDN requests.
  - Replaced mixed Unicode symbols and emoji icons across the telemetry bar, sidebar, map toolbars, modals, and drawers with a unified 16×16 `stroke-width="1.75"` inline SVG icon module (`web/src/icons.ts`).
- **Auto-Matched Regional Overlay Tabs & Cartographic Theme Swatches (`web/src/components/sidebar.ts`, `web/src/presets.ts`):**
  - Scoped **Terrain & Overlays** into `AU | NZ | US | CA` tabs with live active-layer counts that automatically match the selected regional preset instead of dumping all four countries' checkboxes simultaneously.
  - Added 3-strip color-palette swatches to all 4 print cartography theme buttons (`Color`, `B&W Print`, `Night (Red-Light)`, `Alpine`) so operators can preview each palette at a glance.

### Improved & Fixed

- **MapLibre GL JS v6 Production Worker Bundling (`web/src/map.ts`, `web/tests/modal-dom.test.mjs`):**
  - Explicitly wired `maplibregl.setWorkerUrl(...)` to Vite's `?url`-emitted `/assets/maplibre-gl-worker-<hash>.mjs` bundle so `Vector HD` basemaps and live GeoJSON sheet footprints, MGRS grids, viewsheds, RF links, and tactical overlays work reliably in production Docker builds under `worker-src 'self'` (fixing `/assets/maplibre-gl-worker.mjs` HTTP 404s introduced by MapLibre v6's external worker resolution).
- **Information Architecture Cleanup & Error Recovery (`web/src/components/{sidebar,packsModal,authModal,adminModal,jobsDrawer,projectsDrawer}.ts`):**
  - Flattened stacked sidebar cards into clean hairline sections, stripped numbered `01`–`06` and eyebrow noise chips, removed duplicated supplemental-page checkboxes and redundant sidebar tactical tool buttons, disabled the **Elevation** selector in the Pack Manager when DEM download is unchecked, and added an explicit error state with a `Retry` action when `/api/packs/regions` is unreachable.
- **Responsive Mobile Studio (`<= 680px`) (`web/src/sidebarPrefs.ts`, `web/src/main.ts`, `web/src/map.ts`, `web/src/styles.css`):**
  - Defaulted the mobile sidebar to collapsed so the map opens full-screen, added a click-outside scrim overlay and compact mobile basemap `<select>`, auto-collapsed the sidebar when choosing map tools, wrapped the 5-tab Pack Manager cleanly without horizontal clipping, adopted `100dvh` viewport sizing, collapsed compact attribution on load to prevent scale-bar overlap, and enlarged coarse-pointer touch targets.

### Verification

- **Automated & Live Visual Suites (`618` offline pytest + `67` frontend tests + live multi-region E2E):**
  - Verified `618` offline PyQGIS tests (`--network none`), `67` frontend tests (covering mobile sidebar defaults, theme persistence/toggle, `PacksModal` retry recovery, GitHub footer link security attributes, and MapLibre v6 worker URL wiring), live multi-region open-data E2E renders across US/CA/AU/NZ, and zero-console-error Docker Studio visual captures at `1440×900`, `1024×768`, and `390×844` (`docs/images/studio-v18-{dark,daylight}.jpg`).

## [1.7.2] - 2026-11-28

Reliability, maintainability, and test-hardening maintenance release consolidating Studio modal/drawer abort lifecycles, bounding browser API requests with explicit timeout signals, centralizing auth-mode configuration lookup, adding repository-level `anti-slop` guardrails, and substantially expanding mutation-verified frontend and backend test coverage.

### Improved & Hardened

- **Bounded Browser API Timeouts & Shared Modal Lifecycle (`web/src/api.ts`, `web/src/components/{lifecycle,adminModal,packsModal,authModal,projectsDrawer}.ts`, `web/src/{turnstile,deeplink,main}.ts`):**
  - Added a default 90 s `AbortSignal.timeout` fallback composed with caller `AbortSignal`s (`AbortSignal.any`) in `web/src/api.ts` so browser API probes and actions never hang indefinitely while preserving instant workspace-switch cancellation.
  - Extracted shared `linkAbortSignal`, `initModalBackdrop`, and `reportAbortAwareError` helpers into `web/src/components/lifecycle.ts` and unified `AdminModal` user mutations, `TurnstileController` expiry/timeout widget resets, job cancel/delete list updates, and one-time launch URL parameter stripping (`stripOneTimeLaunchParams` in `web/src/deeplink.ts`).
  - Logged caught errors at debug level in the Studio offline-basemap availability probe instead of silently swallowing failures.
- **Centralized Auth-Mode Resolution, CSP & Repository Quality Guardrails (`src/gridforge/web/{app,auth,worker}.py`, `src/gridforge/mcp/http.py`, `.anti-slop/learned/rules.json`):**
  - Allowed the Cloudflare Web Analytics beacon (`https://static.cloudflareinsights.com`) in `Content-Security-Policy` `script-src` while keeping strict no-`unsafe-inline` script rules intact.
  - Centralized `GRIDFORGE_AUTH_MODE` parsing and validation into `gridforge.web.auth.resolve_auth_mode()` across the web app, background worker, and `/mcp` Streamable HTTP router to prevent cross-module configuration drift.
  - Added verified repository-level `anti-slop` learned rules covering subprocess shell safety, safe YAML loading, dynamic `eval`/`exec`, pinned Dockerfile base images, least-privilege file modes, Python HTTP timeouts, and live end-to-end verification discipline.

### Verification

- **Mutation-Hardened Suites (`618` offline pytest + `65` frontend tests + live multi-region E2E):**
  - Expanded frontend DOM/API/Turnstile/deeplink tests (`web/tests/{modal-dom,api-auth-admin,turnstile,deeplink}.test.mjs`), raising diff-scoped TypeScript mutation kill rate across `v1.7.x` changes from `23.3%` to `80.0%`.
  - Expanded backend security and budget boundary tests (`tests/unit/test_{source_work_budgets,preview_work_budgets,auth_and_limits,mcp}.py`), covering COG request-count caps, direct NRCan `_warp_cogs` allowlist validation, default Overpass `follow_redirects=False`, exact WFS/SA ZIP limits, low-res preview DEM routing, symlinked sheet-workspace confinement, and empty `/mcp` client host handling (`95.8%` sources / `91.7%` web+MCP mutation kill rates).

## [1.7.1] - 2026-11-27

Comprehensive security hardening release enforcing explicit hosted API key scopes across HTTP and MCP, confining atlas worker paths and provider/terrain resource budgets, isolating hosted Studio workspaces across account and guest transitions, keeping private map parameters out of browser URLs, and locking down release workflow supply-chain integrity.

### Security & Hardening

- **Hosted API Key Scope Enforcement, Geocoding Budgets & Body Caps (`src/gridforge/web/{app,auth,db,limits,schemas}.py`, `src/gridforge/mcp/{http,server}.py`, `docs/ai-agents.md`, `deploy/hosted-mode.md`):**
  - Enforced explicit per-operation PAT scopes (`planning`, `jobs:read`, `jobs:write`, `projects:read`, `projects:write`, `shares:read`, `shares:write`, and standalone `all`) across all `/api/*` routes and `/mcp` tools, keeping account management, API key CRUD, site administration, offline pack management, and cross-tenant share moderation behind browser-session authentication.
  - Failed closed with `401` on legacy keys carrying empty or unrecognized stored scopes, enforced `projects:read` when sharing saved projects by `project_id`, and forwarded caller `request.client` into in-process MCP `ASGITransport` requests so non-Cloudflare `/mcp` callers retain distinct per-IP rate-limit buckets.
  - Bounded outbound Nominatim geocoding with a single-flight provider slot (`503` + `Retry-After`), per-IP (`GRIDFORGE_GEOCODE_HOURLY_LIMIT=30`) and shared database-wide (`GRIDFORGE_GEOCODE_GLOBAL_HOURLY_LIMIT=300`, `0` disables misses) hourly miss caps in both hosted and local modes, removed raw search queries from logs/usage details, fixed chunked/streamed body overflow handling to return `413` cleanly, and switched hosted job thumbnails and page previews to `Cache-Control: private, no-store`.
- **Atlas Worker Confinement, Terrain Preview Budgets & Bounded Open-Data Reads (`src/gridforge/sources/{_cog_ranges,_http,dem_ca_hrdem,osm_overpass,overlays_au_states,overlays_au_states_west}.py`, `src/gridforge/web/{viewshed,worker,schemas}.py`, `docker-compose*.yml`, `docs/DATA_SOURCES.md`):**
  - Derived atlas worker scratch workspaces, temporary DEM copies, and sheet PDFs strictly from server-owned sheet ordinals (`sheet_01`, ...) under resolved `tmp_work_dir` and stripped ASCII control characters from `StripSheetInput.sheet_id`.
  - Bounded terrain previews before source selection (`<= 100 km` RF links, `<= 125 km` per side and `<= 8,000 km²` DEM footprints, antimeridian crossing rejection) and routed live previews through regional non-LiDAR tiers while preserving geodesic-only ruler fallback for wide paths.
  - Added a loopback single-range COG proxy (`_cog_ranges.py`) for NRCan Canadian DEM reads strictly allowlisting NRCan's two AWS Canada Central S3 buckets with redirect refusal, byte/request/time budgets, and clean timeout shutdown.
  - Bounded Australian/NZ WFS GeoJSON and SA overlay downloads with streaming byte/feature/geometry/time limits, strict redirect validation (HTTPS-only, private/loopback IP rejection, same-host default with explicit `waterconnect.sa.gov.au` subdomain allowlist for SA trails), and direct raw-deflate + local-header/CRC validation of the SA trails ZIP archive to eliminate Python `zipfile` vs GDAL `/vsizip/` forged-`file_size` ZIP-bomb differentials.
  - Defaulted Overpass to the configured primary endpoint (`OVERPASS_URL`) with no third-party fallback mirrors unless operators explicitly opt in via `OVERPASS_FALLBACK_URLS`, and declined HTTP redirects on Overpass POST queries.
- **Hosted Studio Workspace Isolation & URL Privacy (`web/src/`, `src/gridforge/web/{auth,schemas}.py`):**
  - Reset map camera/layers, uploaded overlay files, form drafts, jobs/projects drawers, Turnstile generation, toasts, and open modals on hosted identity changes, aborting all in-flight requests so prior-account responses cannot populate the next workspace.
  - Added an opaque HMAC-derived `workspace_scope` to `GET /api/auth/session` so even `Guest → Guest` cookie expiry/resets trigger full workspace resets, deferred 401 Sign-In modal opening until session refresh completes, guarded `window` `keydown` shortcuts while `changeSession()` runs, added cross-tab `BroadcastChannel` sync (including after Google OAuth sign-in), and gave `AdminModal`/`PacksModal` self-contained `AbortController` disposal.
  - Consumed incoming legacy deep-link query parameters once on load and removed private map details from `window.location` while keeping deliberate `?share=` snapshots and explicit sharing intact.
- **Release Pipeline & Supply-Chain Integrity (`.github/workflows/release.yml`, `examples/render-examples.sh`, `docs/RELEASE_CHECKLIST.md`, `.agents/skills/gridforge-release/SKILL.md`):**
  - Hardened `.github/workflows/release.yml` to verify SemVer release tags point to commits on the default branch, pin all GitHub Actions by full commit SHA, scope `packages: write` strictly to `build-and-push`, check out immutable commit SHAs, record `org.opencontainers.image.revision`, pull sample-render images by exact published OCI digest, reserve `:vX.Y` and `:latest` for stable tags, and pin `examples/render-examples.sh` to a verified multi-arch GHCR digest.

### Added & Fixed

- **Cloud Dev Workflow & Community Governance (`deploy/cloud-dev.md`, `.devcontainer/`, `SECURITY.md`, `CODE_OF_CONDUCT.md`):**
  - Added `SECURITY.md` vulnerability disclosure & hardening architecture documentation, `CODE_OF_CONDUCT.md`, and cloud VM / Dev Container / CI offloading documentation and bootstrap scripts.
  - Narrowed base64 error handling and logged caught errors cleanly in the Studio frontend and interactive MCP App (`web/src/`, `web/mcp-app/`).

### Verification

- **Automated Suites (`610` offline pytest + `54` frontend tests + live multi-region E2E):**
  - Added `tests/unit/test_preview_work_budgets.py`, `tests/unit/test_source_work_budgets.py`, and expanded `test_agent_api.py`, `test_shares_api.py`, `test_web_api.py`, `test_auth_and_limits.py`, `web/tests/deeplink.test.mjs`, `web/tests/modal-dom.test.mjs`, and `web/tests/turnstile.test.mjs`.
  - Verified `610` offline PyQGIS pytest tests (`--network none`), `54` frontend tests + production Studio/MCP App builds (`0` `npm audit --omit=dev` vulnerabilities), and live multi-region renders including NRCan HRDEM COG range-proxy reads, SA Trails ZIP redirect/validation, and VIC/NT WFS overlays.

## [1.7.0] - 2026-11-27

AI-native integration and interactive chat UI release adding a zero-extra-dependency Model Context Protocol (MCP) server (`stdio` + Streamable HTTP `/mcp`), SEP-1865 interactive MCP Apps (`ui://gridforge/app.html` with Antigravity inline, side-pane, and sidebar entrypoints), Pillow PNG visual cards, scoped Bearer Personal Access Tokens (`gf_...`), HMAC-signed artifact/preview URLs, multi-page PDF previews, synchronous render waits (`POST /api/jobs?wait=true`), machine-readable discovery (`/api/capabilities`, `/llms.txt`, complete `/openapi.json`), CLI `--spec-file`, and a reusable Agent Skill + Antigravity plugin.

### Added

- **AI-Ready Web API, Scoped Bearer PATs & Signed URLs (`src/gridforge/web/{app,auth,capabilities,db,schemas}.py`):**
  - Added SQLite-backed Personal Access Tokens (`GET/POST/DELETE /api/auth/api-keys`, `gf_...` prefix, SHA-256 hashed at rest, plaintext returned once, 25 active-key per-user cap) for `Friend` and `Admin` users in hosted mode (`Authorization: Bearer` or `X-API-Key`), bypassing guest-only Cloudflare Turnstile while preserving full per-role quotas, DPI/grid caps, and tenant isolation; plus optional static `GRIDFORGE_API_KEY` protection for single-tenant `auth_mode=none` deployments.
  - Added short-lived HMAC-signed artifact download & page-preview URLs (`?sig=...`, `GRIDFORGE_SIGNED_URL_TTL_SECONDS=3600`), multi-page PDF rasterization (`GET /api/jobs/{job_id}/pages` and `GET /api/jobs/{job_id}/pages/{page}.png?width=...`), and synchronous render completion polling (`POST /api/jobs?wait=true&timeout_s=120`, returning `200 OK` on terminal state or `202 Accepted` if still running).
  - Added machine-readable `GET /api/capabilities`, plain-text `GET /llms.txt` discovery, and complete `/openapi.json` schemas (`CreateJobRequest`, `StripPlanRequest`, `BearerAuth` / `ApiKeyHeader`, optional `servers` via `GRIDFORGE_PUBLIC_URL`, and `operationId`/summaries across all endpoints).
- **Native Zero-Extra-Dep MCP Server & Pure-Pillow Visual Cards (`src/gridforge/mcp/`, `src/gridforge/cli.py`):**
  - Added dual-transport MCP server (`gridforge mcp` over `stdio` + mounted FastAPI Streamable HTTP `/mcp`, supporting protocol versions `2025-06-18`, `2025-03-26`, and `2024-11-05`) with **14 model-visible tools** (`open_gridforge_studio`, `answer_location_briefing`, `geocode_place`, `preview_footprint`, `analyze_rf_link`, `compute_viewshed`, `measure_route`, `plan_strip_atlas`, `render_map`, `get_job_status`, `get_job_page_preview`, `create_share_link`, `list_packs`, `get_capabilities`), **3 app-only tools** (`app_get_job_status`, `app_get_job_page`, `app_get_capabilities`), **3 resources**, and **4 guided prompts**.
  - Added fast pure-Pillow dark-tactical PNG visual cards (`src/gridforge/mcp/visuals.py`) for footprints, viewsheds, route cards, and 760×360 curved-Earth 1st Fresnel RF charts so *every* MCP host renders crisp inline graphics alongside natural-language answers.
  - Added declarative JSON rendering via `gridforge render --spec-file <path|->` for shell-based coding agents.
- **Interactive SEP-1865 MCP App Chat UI (`web/mcp-app/`, `src/gridforge/mcp/assets/mcp_app.html`, `Dockerfile`):**
  - Built a self-contained single-file HTML/JS interactive MCP App (`ui://gridforge/app.html`, MIME `text/html;profile=mcp-app`, CSP `default-src 'none'` safe, dark/light host theme reactive) with a pan/zoom UTM & 1 km MGRS map canvas, SVG WMM2025 True/Grid/Magnetic North declination compass, hover-scrubbing 1st Fresnel RF chart with live band & antenna height recalculation, terrain viewshed & route/strip views, live-polling multi-page printable PDF viewer, and Antigravity `aux_pane` (**GridForge Studio**) + `global` (**GridForge Maps**) entrypoints.
- **Documentation, Agent Skill & Antigravity Plugin (`docs/ai-agents.md`, `docs/agent-skill/SKILL.md`, `plugins/antigravity/`, `deploy/hosted-mode.md`):**
  - Added end-to-end setup guides for Claude Desktop/Code, OpenAI Codex, Google Antigravity, ChatGPT Custom Actions/Remote MCP, Cursor, and VS Code, plus a drop-in Antigravity plugin (`plugins/antigravity/`) and portable Agent Skill (`docs/agent-skill/SKILL.md`).

### Security

- **Hosted Edge-Proxy, Subprocess & DoS Hardening (`src/gridforge/mcp/{http,server}.py`, `src/gridforge/subprocess_env.py`, `src/gridforge/web/{app,auth}.py`):**
  - Enforced `OriginProxyAuthMiddleware` on `/mcp` while safely forwarding verified origin-proxy, `CF-Connecting-IP`, and `X-Forwarded-Proto` headers exclusively into in-process ASGI sub-requests (never external proxy URLs).
  - Stripped `GRIDFORGE_API_KEY` and `*API_KEY*` variables from GIS/PDF subprocesses (`build_safe_subprocess_env`), guarded `pdftoppm` page preview rendering with per-job locks, bounded concurrency slots, and safe environments, capped JSON-RPC batches at 20 messages, rejected forged boolean `exp` claims in signed tokens, enforced a 25 active PAT per-user limit, scrubbed raw internal exceptions from `/mcp` errors, and expanded `scripts/release_preflight.py` privacy scanning to `web/mcp-app/`, `docs/`, and `plugins/`.

### Verification

- **Automated Suites (`575` offline pytest + `49` frontend tests + `26` live multi-region E2E):**
  - Added `tests/unit/test_agent_api.py`, `tests/unit/test_mcp.py`, `tests/unit/test_mcp_e2e_app.py`, and `web/tests/mcp-app.test.mjs` verifying Bearer PAT lifecycle, cross-tenant `/mcp` isolation, `OriginProxyAuthMiddleware` forwarding, signed URLs, page previews, sync wait, OpenAPI completeness, all 17 MCP tools/resources/prompts, and interactive MCP App rendering.

## [1.6.1] - 2026-11-27

Comprehensive security hardening, container least-privilege, and end-to-end GIS/web performance optimization release upgrading MapLibre GL JS to v6, isolating web secrets from GIS subprocesses, hardening proxy/origin verification, bounding hosted storage & DoS vectors, and substantially accelerating terrain derivation, PyQGIS styling, exports, and web/Studio responsiveness.

### Security & Hardening

- **Non-Root Multi-Stage Container & Secret Isolation (`Dockerfile`, `.dockerignore`, `src/gridforge/subprocess_env.py`, `src/gridforge/web/entrypoint.py`):**
  - Split `Dockerfile` into `test` and lean non-root `runtime` (`USER 10001:10001`, zero `pytest` in production) with `.dockerignore` and automatic root-to-`10001` volume ownership handoff when launched with `RAILWAY_RUN_UID=0`.
  - Added allowlisted `build_safe_subprocess_env()` stripping web/cloud credentials (`GRIDFORGE_SESSION_SECRET`, admin/OAuth/Turnstile/origin secrets, AWS/GitHub/Railway tokens) from worker, PyQGIS, supplement, and GDAL/OGR subprocesses, and disabling GDAL `/vsicurl/` (`CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.nonexistent`) for untrusted overlay parsing.
- **Origin-Proxy Auth, Private Proxy Trust & Strict CSP (`src/gridforge/web/{access,app,auth,limits}.py`, `web/src/`):**
  - Replaced `FORWARDED_ALLOW_IPS=*` defaults with private proxy CIDRs, stopped auto-trusting `RAILWAY_PUBLIC_DOMAIN` when `GRIDFORGE_ALLOWED_HOSTS` is explicitly set, and added `OriginProxyAuthMiddleware` (`GRIDFORGE_ORIGIN_PROXY_SECRET` / `X-GridForge-Origin-Secret`) with constant-time comparison while permitting Railway's internal `/api/health` probe.
  - Added strict HTTP security headers (`Content-Security-Policy`, HSTS, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`), eliminated inline JS/event handlers across the Studio SPA (`<script id="gf-brand" type="application/json">`), disabled `/docs`/`/redoc`/`/openapi.json` in hosted mode (`404`, opt-in via `GRIDFORGE_ENABLE_DOCS=true`), enforced global request body caps and per-account login throttling, and scrubbed internal paths/tracebacks from hosted errors.
- **Upload, Injection, Remote Fetch & Dependency Hardening (`web/package.json`, `pyproject.toml`, `src/gridforge/sources/`, `src/gridforge/render/layout.py`):**
  - Upgraded `maplibre-gl` (`5.24.1` → `^6.19.0`) patching critical XSS `GHSA-jrc7-96c5-q579` (`0` production `npm audit` vulnerabilities) and bumped `pypdf>=7.0.1`, `pillow>=13.0.0`, `fastapi>=0.115.12`, `h11>=0.16.0`, and `python-multipart>=0.0.31`.
  - Hardened `UserOverlaySource` (`overlay_user.py`) against XXE/billion-laughs XML, KMZ zip bombs, symlink escapes, and `/vsicurl/` SSRF; added QGIS expression/XML escaping in `layout.py` and `declination.py`; added explicit timeouts, HTTPS/hostname allowlists, disk-to-disk streaming caps, and secret-redacted logging across remote DEM/OSM/overlay fetchers; hardened pack builds with 4° custom span caps, atomic staging, and duplicate-build locking.
- **Hosted Storage Bounds & DoS Prevention (`src/gridforge/web/{projects,worker,db}.py`, `web/src/main.ts`):**
  - Bounded anonymous Guest saved projects with a `64 KB` payload cap, per-guest cap (`GRIDFORGE_MAX_PROJECTS_PER_GUEST=5`), hourly rate limit (`GRIDFORGE_GUEST_PROJECT_HOURLY_LIMIT=20`), and global cap (`GRIDFORGE_MAX_GUEST_PROJECTS=500`) with automatic expired/oldest eviction on insert.
  - Wired `GRIDFORGE_ADMIN_TTL_HOURS` (`720` default in hosted mode, `0` disables) into `worker.py` and `db.py` to protect bounded persistent volumes, and stopped auto-firing up to 30 DEM preview requests on `?share=` / deep-link load in Studio (explicit user-triggered Compute buttons).
  - Extended `scripts/release_preflight.py` privacy scanning to `src/`, `tests/`, and `web/src/`, hardened `scripts/render_release_samples.sh` tar extraction flags, and documented all new env vars in [`deploy/hosted-mode.md`](deploy/hosted-mode.md).

### Performance

- **Web API, SQLite & Studio Frontend (`src/gridforge/web/{db,app}.py`, `src/gridforge/cli.py`, `web/src/`):**
  - Added thread-local SQLite connection pooling, composite job/usage indexes, and single-query `get_admin_usage_report()` aggregation; added `GZipMiddleware`, immutable `1 yr` caching on content-hashed `/assets/*`, and mtime-fingerprinted pack discovery caching.
  - Added visibility-aware background polling and lazy-loaded `PacksModal` / `AdminModal` bundles in Studio, plus lazy CLI imports in `cli.py`.
- **GIS Pipeline, Terrain, PyQGIS & Exports (`src/gridforge/{geo,pipeline,render,export,packs}/`):**
  - Cached WGS84↔UTM transformers (`src/gridforge/geo/crs.py`), added `PointDeclination` fast path (`src/gridforge/geo/declination.py`), batched DEM elevation sampling in waypoints/tactical overlays, added OGR transaction batching across vector builders, `-spat` R-Tree pre-filtering in AU/US overlay fetchers, `-gt 65536` + attribute indexes in `src/gridforge/pipeline/normalize.py`, and schema-verified `setSubsetString` filters in `src/gridforge/render/styles.py`.
  - Accelerated `src/gridforge/pipeline/terrain.py` with threaded GDAL, skipped redundant UTM warps, eliminated intermediate `dem_utm_ft.tif`, ran hillshade/contours/slope/viewshed concurrently, and downsampled LiDAR viewsheds to 10 m.
  - Batched supplement PDF generation into a single subprocess (`src/gridforge/geo/supplements.py`), parallelized fetch/normalize/pack stages (`src/gridforge/pipeline/runner.py`, `src/gridforge/packs/builder.py`, `src/gridforge/packs/basemap_builder.py`), shared `EPSG:3857` warping and MBTiles reuse across MBTiles/GeoPackage/OsmAnd exports, parallelized Garmin KMZ JPEG encoding, and streamed chunked `QImage` → GeoTIFF writes in `src/gridforge/export/raster.py`.

### Verification

- **Automated Suites (`553` offline pytest + `44` frontend tests + `26` live multi-region E2E):**
  - Added `tests/unit/test_security_and_perf_hardening.py` covering secret isolation, injection/XXE/SSRF defenses, proxy/origin lockdown, bounded guest projects, admin TTL retention, and connection pooling, and verified `553` offline pytest cases + `26` live multi-region open-data E2E renders across US, Canada, Australia, and New Zealand.

## [1.6.0] - 2026-11-27

Deep-linking and shareable trip snapshot release expanding stateless URL permalinks to preserve full Studio views (basemap, zoom, cartography, outputs, booklet toggles, waypoints, RF links, tactical graphics, and custom LOS observer labels), adding a one-click topbar **Share** popover, and introducing SQLite-backed immutable short share links (`?share=<token>`, `/api/shares`) for both live Studio views and Saved Trip Projects.

### Added

- **Expanded Stateless URL Deep Links (`web/src/deeplink.ts`, `web/src/main.ts`, `web/src/map.ts`):**
  - Preserves backward compatibility with all existing query parameters while adding compact, non-default encoding for **basemap** (`bm`, including deferred `offline` basemap activation), **map zoom** (`z`), **cartography controls** (`contours`, `hillshade`, `labels`, `hide` for disabled layers), **output formats** (`out`, `mbfmt`, `garmin`, `dgrid`), **booklet toggles** (`rc`, `ep`, `rfcard`), custom **LOS observer labels** (`los`), **waypoints** (`wp`), **RF links** (`rf`), and **tactical graphics** (`tac` base64url JSON).
  - Syncs the browser address bar across every state mutation (including basemap switching, debounced zoom changes, waypoints, RF links, and tactical graphics), auto-hydrates markups/route metrics/viewsheds/RF profiles on load (respecting Cloudflare Turnstile), and enforces strict input sanitization plus a `3800`-character URL budget with graceful markup fallback.
  - Persists and restores `basemap` and `map_zoom` inside Saved Trip Projects alongside full configuration state.
- **Topbar Share Button & Accessible Share Popover (`web/src/components/telemetry.ts`, `web/src/styles.css`):**
  - Added a topbar **Share** trigger next to **Projects** with live sheet/markup summary, **Copy Direct Link**, **Create Short Share Link**, truncation & local GPX/KML notices, keyboard Escape/click-outside dismissal, and responsive icon-only collapse on narrow viewports.
- **Server-Backed Shareable Snapshots (`/api/shares`, `?share=<token>`, `src/gridforge/web/shares.py`):**
  - Added SQLite `shared_snapshots` storage with 96-bit URL-safe tokens, allowlisted state keys, collection bounds, and a `64 KB` payload cap (`413` on overflow).
  - Added `POST /api/shares` (snapshot from current Studio state or an owned `project_id` with tenant ownership `403` and `SameOriginWritesMiddleware` CSRF protection), public read-only `GET /api/shares/{share_id}` (stripping owner/tenant metadata, setting `Cache-Control: no-cache`, and returning `404` once expired or revoked), owner-scoped `GET /api/shares`, and owner/admin `DELETE /api/shares/{share_id}`.
  - Added role-aware hosted TTL caps (`GRIDFORGE_GUEST_SHARE_TTL_HOURS=168` / 7d, `GRIDFORGE_FRIEND_SHARE_TTL_HOURS=720` / 30d; Admin/local permanent by default), hourly creation rate limits (`GRIDFORGE_SHARE_HOURLY_LIMIT=30`, `GRIDFORGE_FRIEND_SHARE_HOURLY_LIMIT=120`), and automatic expired snapshot pruning in `db.prune_expired_shares()` / `worker.run_pruning_pass()`.
  - Added a one-click **Share** action on every Saved Trip Project card (`web/src/components/projectsDrawer.ts`) and automatic `?share=<token>` hydration on Studio load with hosted-tier capability clamping.
- **Automated Verification (`538` offline pytest + `44` frontend tests + `26` live multi-region E2E):**
  - Added `web/tests/deeplink.test.mjs`, `tests/unit/test_shares_api.py`, and Share API/popover/Projects Drawer tests in `web/tests/api-auth-admin.test.mjs` and `web/tests/modal-dom.test.mjs`, and documented the new hosted share environment variables in [`deploy/hosted-mode.md`](deploy/hosted-mode.md).

### Fixed

- **Multi-Sheet Atlas Index URL Persistence:** Explicitly unchecking **Atlas Index** on `2×2`–`3×3` grids now emits `index=0` in deep-link URLs instead of omitting `index` and re-enabling the cover page on reload.

## [1.5.0] - 2026-11-27

High-resolution cartography, public hosted multi-tenant deployment, and Studio UX upgrade release adding `1:10,000` / `1:12,500` scales, `300` / `450` / `600 DPI` print and digital tile controls, true `1.0 m` LiDAR terrain & `2 m` / `5 ft` contours, richer OSM backcountry micro-features across all four themes, a zero-key **Vector HD** (OpenFreeMap) Studio basemap, a resizable/collapsible sidebar & redesigned header, and opt-in public hosted mode (`GRIDFORGE_AUTH_MODE=hosted`, powering [GridMaps](https://gridmaps.net)).

### Added

- **Higher Print & Digital Export Resolution (`300` / `450` / `600 DPI`, `--export-dpi`):**
  - Exposed inline **Print PDF DPI** and **Digital Export Tiles DPI** selectors in Studio (`web/src/components/sidebar.ts`), wired through URL permalinks, Saved Projects, Copy CLI (`--dpi`, `--export-dpi`), and backend job execution (`src/gridforge/cli.py`, `src/gridforge/web/worker.py`).
  - Added role-aware DPI caps (`GRIDFORGE_GUEST_MAX_DPI=300`, `GRIDFORGE_FRIEND_MAX_DPI=600`) in `src/gridforge/web/limits.py` and `web/src/session.ts`.
- **Finer Tactical Scales (`1:10,000`, `1:12,500`), True `1.0 m` LiDAR Terrain & `2 m` / `5 ft` Contours:**
  - Expanded `VALID_SCALES` to `(10000, 12500, 15000, 24000, 25000, 48000, 50000)` with calibrated metric/imperial collar scale bars (`src/gridforge/render/layout.py`) and 100 mm Calibration Romer scale notes (`src/gridforge/geo/supplements.py`).
  - Preserved true **`1.0 m` LiDAR** terrain on `<= 1:15,000` sheets with a `4096 px` memory safety cap (`src/gridforge/pipeline/terrain.py`), unlocked **`2 m` / `5 ft`** high-density contours on high-resolution DEMs (`<= 10 m`), and upgraded Queensland `QldDem` fetching (`src/gridforge/sources/dem_au_elvis.py`) to `1.0 m` (`qld-1m-lidar`) within verified `4096 × 2048 px` ImageServer limits.
- **Richer OSM Backcountry Cartography Across All 4 Themes:**
  - Expanded `config/osmconf.ini`, `src/gridforge/pipeline/normalize.py`, and `src/gridforge/sources/osm_overpass.py` (`v4:` cache key) to capture cliffs/escarpments, waterfalls/rapids, natural springs, stream fords, cave entrances, survey points/trigs, cairns/rock outcrops, gates/stiles/cattle grids, walls/fences, and historic ruins/forts.
  - Styled all new backcountry features plus distinct `highway=steps` and rough `tracktype=grade4|grade5` 4WD tracks across `color`, `bw`, `night-red` (preserving `R <= 61` red-headlamp legibility), and `alpine`, regenerating `transport.qml` and `points.qml` across all 4 themes.
- **Retina Interactive Studio Basemap (`Vector HD`), Redesigned Header & Resizable Sidebar:**
  - Added a lazy-loaded, zero-API-key **Vector HD** basemap powered by **OpenFreeMap Liberty** (`web/src/map.ts`) preserving all 18 GeoJSON overlays and instant toggling with OpenTopoMap, OSM Standard, Esri Imagery, and Offline MBTiles.
  - Redesigned the Studio header (`web/src/components/telemetry.ts`) with zero mid-character telemetry clipping and an accessible monogram account dropdown menu, and added a persistent, drag-resizable (`300px` min), hide/show sidebar (`[` hotkey, `web/src/sidebarPrefs.ts`).
- **Opt-In Hosted Multi-Tenant Mode (`GRIDFORGE_AUTH_MODE=hosted`), Custom Branding & Single-Container Deployment:**
  - Added SQLite-backed `Guest` / invite-only `Friend` / `Admin` roles (`src/gridforge/web/{auth,limits,cache_prune,entrypoint}.py`), email/password + Google OAuth, Cloudflare Turnstile bot protection, strict per-owner job/project isolation, Admin usage dashboard & public-render kill switch, role-aware artifact TTL + LRU cache pruning, configurable SSR/collar branding (`GRIDFORGE_BRAND_NAME`, `GRIDFORGE_BRAND_SUBTITLE`, `GRIDFORGE_BRAND_TAGLINE`), single-container Railway config (`railway.toml`), and a dedicated deployment guide ([`deploy/hosted-mode.md`](deploy/hosted-mode.md)) while preserving zero-login `127.0.0.1` local defaults (`GRIDFORGE_AUTH_MODE=none`).
- **Automated Verification (`534` offline pytest + `35` frontend tests):**
  - Added hosted E2E suites (`tests/e2e/`) and unit tests (`tests/unit/test_auth_and_limits.py`, `test_container_entrypoint.py`, `test_db_tenancy.py`, `test_worker_pruning.py`, plus `web/tests/*.test.mjs`) and mounted repo configs + `web/src` in `.github/workflows/ci.yml` so all 534 offline backend tests run in CI.

## [1.4.0] - 2026-11-26

Major tactical communications, multi-theme cartography, North America backcountry coverage, offline basemap, and field-device export release shipping four cartographic themes (`172` native QGIS 3.40 `.qml` styles), curved-Earth RF Fresnel Link Cards, tactical range rings & sectors, GeoPackage / OsmAnd / Field Bundle `.zip` exports, Saved Trip Projects, turnkey pure-GDAL offline basemaps across 29 global regions, and full US & Canada backcountry open-data sources.

### Added

- **Phase 0 — Live Multi-Arch GHCR Release Pipeline & Studio Polish:**
  - Added `workflow_dispatch` tag trigger (`tag` input + semver resolution) to `.github/workflows/release.yml` and published live multi-arch (`linux/amd64` + `linux/arm64`) images to `ghcr.io/jremick/gridforge`.
  - Auto-clears Copy CLI / form error banners in Studio on control edits and organizes Quick-Jump Presets into 4 regional tabs (`NSW/VIC`, `AU`, `NZ`, `NA` — 32 curated backcountry destinations).
- **Track 1 — Point-to-Point Curved-Earth RF / 1st Fresnel Link Profile & Link Card PDF (`--rf-link`, `--rf-link-card`, `POST /api/rf-link-profile`):**
  - Curved-Earth (`k=4/3`) terrain & 1st Fresnel zone clearance engine (`src/gridforge/pipeline/terrain.py`) computing `CLEAR` / `MARGINAL` / `OBSTRUCTED` status, 60% F1 clearance, exact FSPL (dB), True/Grid/WMM2025 Magnetic bearings, NATO mils, and 10 m MGRS endpoints across 200 bilinear DEM samples.
  - Pure-vector **RF / LOS Fresnel Link Card** companion PDF page (`render_rf_link_card_pdf` in `src/gridforge/geo/supplements.py`) and interactive MapLibre Studio `⚡ A ↔ B` picker with band presets (`LoRa 915`, `UHF CB 477`, `GMRS 462`, `VHF 146`, `LoRa 868`, `2.4 GHz`, `5.8 GHz`) and live inline SVG Fresnel profile chart.
- **Track 2 — Turnkey Pure-GDAL Offline Basemap Builder & 29-Region Global Pack Manager (`gridforge basemap-pack`, `include_basemap`):**
  - Pure GDAL/NumPy/PIL hypsometric color-relief + multi-directional hillshade Web Mercator `.mbtiles` builder (`src/gridforge/packs/basemap_builder.py`, zoom `7–12`, atomic via `staged_archive`, `< 300 MB` RAM, zero QGIS/Qt imports).
  - Expanded offline pack catalog (`src/gridforge/packs/regions.py`) to **29 grouped regions** (`au_tactical`: 7, `au_state`: 8, `nz`: 6, `na`: 8 — 5 US + 3 Canada) with `include_basemap=True` bundling `<pack_dir>/basemap.mbtiles` and per-pack discovery (`GET /api/basemap/info?pack_id=` & `GET /api/basemap/{z}/{x}/{y}.{ext}?pack_id=`).
- **Track 3 — `night-red` & `alpine` Themes (`172` Native QGIS `.qml`s) + Tactical Ruler, Range Rings & Sectors (`--mode night-red|alpine`, `--range-ring`, `--tactical-line`, `POST /api/ruler-metrics`):**
  - **`night-red`:** Red-headlamp-safe cartographic theme with strictly low-Red (`R <= 61`) ink on all critical terrain, road, trail, water, grid, viewshed, and tactical layers so nothing disappears under red night-vision illumination.
  - **`alpine`:** Winter/ski-mountaineering theme with snow canvas, glacier/snowfield polygons, Swiss glacier-blue contours, and boosted `0.68` avalanche slope shading.
  - Regenerated all **43 layer roles × 4 themes = 172 native QGIS 3.40 `.qml` files**.
  - Added interactive Studio `📏 Ruler` (live geodesic distance, True/Grid/Mag bearing + NATO mils + DEM `%` grade + `Pin to PDF`), `◎ Ring` concentric geodesic range rings, and `⬠ Sector` polygon AOI markup (`src/gridforge/geo/tactical.py`) rendered crisply onto print sheets.
- **Track 4 — OGC GeoPackage Tiles, OsmAnd `.sqlitedb`, Field Bundle `.zip` & Saved Trip Projects (`--gpkg`, `--osmand`, `--field-bundle`, `/api/projects`):**
  - Atomic OGC GeoPackage raster tiles (`.gpkg`, `GoogleMapsCompatible` EPSG:3857 + overviews), OsmAnd `.sqlitedb` (`BigPlanet` `z=17-zoom`), and `<slug>_field_bundle.zip` bundling all enabled artifacts alongside valid GPX 1.1 (`waypoints`, `viewsheds`, `rf_links`, `tactical_graphics`) and `README_FIELD_BUNDLE.txt`.
  - Added SQLite WAL **Saved Trip Projects** CRUD (`GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}` under `SameOriginWritesMiddleware`), `GET /api/jobs/{id}/thumbnail.png`, and Studio **Projects** slide-over drawer (Save/Load/Clone/Delete with map sheet thumbnails).
- **Track 5 — Strong North America Coverage (US Trails/Public Lands & Canada NRCan HRDEM/Parks Canada):**
  - **United States (`--us-overlay us-trails`, `--us-overlay us-public-lands`):** Live Public Domain **USFS** (`EDW_TrailNFSPublish_01`, `EDW_RecreationOpportunities_01`, `EDW_Wilderness_01`), **NPS** (`NPS_Public_Trails`, `NPS_Public_POIs_Geographic`), and **USGS PAD-US** federal land managers & wilderness boundaries.
  - **Canada (`--dem-source ca-hrdem`, `--ca-overlay ca-canvec-trails`):** Live OGL-Canada **NRCan CanElevation STAC** (`hrdem-mosaic-1m` -> `hrdem-mosaic-2m` -> nationwide `mrdem-30` in `CGVD2013` -> Copernicus GLO-30) plus **Parks Canada** trails/POIs, **DataBC** active forestry recreation trails (BC), and **ECCC CPCAD** protected areas.
  - Upgraded `AutoDEMSource` to accurate **5-way** routing (`US -> CA -> AU -> NZ -> Global`) with piecewise US–Canada international border classification.
- **Automated Verification (`293` offline pytest + `6` Copy CLI + `3/3` live US/Canada/AU E2E):**
  - Added `tests/unit/test_v14_rf_and_na.py`, `tests/unit/test_v14_exports_and_packs.py`, `tests/unit/test_v14_web_and_rf.py`, and `tests/integration/test_v14_features.py` verifying Yosemite CA (`alpine`), Banff AB (`night-red`, `CGVD2013`), and Mt Kosciuszko (RF Fresnel Link Card, Tactical Rings/Sector, `.gpkg`/`.sqlitedb`/`.zip`).

## [1.3.0] - 2026-11-26

Major operational, geospatial, self-hosting, and hardening release adding RF/LOS Viewshed mapping, New Zealand 1 m LiDAR & DOC trails, Queensland Tier-0 2 m State LiDAR, Atlas Overview Index Covers & Fold/Join Marks, Studio Offline Pack Manager & Air-Gapped Basemap Mode, multi-arch GHCR CI/CD + homelab reverse-proxy guides, and five production security/reliability hardening improvements (Codex PRs `#1`–`#5`).

### Added

- **RF / Line-of-Sight Viewshed Mapping (`--viewshed`, `POST /api/viewshed-preview`, Studio `📡 LOS` / `V` hotkey):**
  - Multi-observer `gdal_viewshed` terrain visibility engine (`src/gridforge/pipeline/terrain.py`) rendering translucent emerald LOS coverage on the bottom raster stack (keeping all contours, roads, trails, and MGRS lines pure vector) paired with dashed range rings and `observer.svg` antenna beacons with AGL height callouts (`22` SVG icons, `78` native QGIS 3.40 `.qml` styles).
  - Interactive MapLibre Studio `📡 LOS` tool drops observers directly on the map, previews terrain-masked coverage (`Visible: XX.X km²`), displays 10 m MGRS and ground elevation, and tunes observer height, target height, and max range (`250 m`–`25 km`) before printing.
- **New Zealand Support — LINZ 1 m LiDAR / 8 m DEM (`NZVD2016`) & DOC Trails/Huts/Peaks (`--dem-source nz-linz`, `--nz-overlay nz-doc-trails`):**
  - Streams zero-auth LERC COGs directly from AWS Open Data `s3://nz-elevation` via closed-form NZTM2000 Topo50 / 1:10k indexing (`1 m LiDAR -> 8 m DEM -> Copernicus GLO-30` fallback) and integrates into 4-way `AutoDEMSource` (`US -> AU -> NZ -> Global`).
  - Live CC BY 4.0 **Department of Conservation (DOC)** Great Walks, walking tracks, alpine huts, campgrounds, and **Toitū Te Whenua LINZ** `NZ_Maunga` peaks (plus optional `LINZ_API_KEY` geodetic survey marks) rendered via the unified backcountry trail/POI schema.
- **Australia Tier-0 Queensland 2 m State LiDAR (`dem_au_elvis.py`):**
  - Automatically upgrades Queensland sheets inside completed `QldDem` LiDAR projects to **2 m State LiDAR (`AHD`, CC BY 4.0)** with shoreline cliff-ratio guards before seamlessly falling through to GA National 5 m LiDAR WCS and DEA SRTM 1 s.
- **Atlas Overview & Sheet Index Cover Page + Edge Fold/Join Marks (`--atlas-index`, `--fold-marks`):**
  - Prepends a mathematically exact pure-vector **Atlas Overview & Sheet Index Cover** (`src/gridforge/geo/supplements.py`) showing scaled sheet boxes (`A1..C3` or `S1..Sn`), diagonal overlap hatching, GPX/GeoJSON route polyline, North arrow, scale bar, and a complete 10 m MGRS & UTM extent table.
  - Adds optional `4.0 mm` L-shaped corner registration crop marks and midpoint `FOLD / JOIN` edge ticks outside the neatline strictly clear of collar metadata and MGRS labels.
- **Studio Offline Pack Manager & Air-Gapped Web Basemap Mode (`src/gridforge/web/{basemap,pack_tasks}.py`, `packsModal.ts`):**
  - One-click **Manage Packs** modal in Print Studio backed by `GET /api/packs/regions` (all 15 Australian presets + custom BBox), single-concurrency `POST /api/packs/build` with live stage polling (`GET /api/packs/tasks`), and path-traversal-hardened `DELETE /api/packs/{pack_id}`.
  - Zero-external-request **Air-Gapped Web Basemap Mode**: mount any raster `.mbtiles` archive (`GRIDFORGE_BASEMAP_MBTILES` or `/data/basemap.mbtiles`) to serve XYZ->TMS flipped tiles via `GET /api/basemap/info` & `GET /api/basemap/{z}/{x}/{y}.{ext}` with an automatic **Offline** switcher pill in MapLibre Studio.
- **OSS CI/CD, Prebuilt Multi-Arch GHCR Images & Homelab/Contributor Docs:**
  - Added `.github/workflows/ci.yml` (Node 22 Copy CLI tests + SPA build + Buildx Docker offline pytest suite) and `.github/workflows/release.yml` (multi-arch `linux/amd64,linux/arm64` push to `ghcr.io/jremick/gridforge:{latest,vX.Y,vX.Y.Z}` on tag push).
  - Added zero-build `docker-compose.ghcr.yml`, homelab HTTPS guides (`deploy/Caddyfile.example`, `deploy/traefik-compose.override.yml`, `deploy/tailscale-serve.md`), `CONTRIBUTING.md`, `docs/STYLING_GUIDE.md`, and GitHub issue/PR templates.

### Fixed & Hardened (Codex PRs `#1`–`#5`)

- **Local-First Studio Security Defaults (PR `#1`):** Bound Compose ports and `gridforge serve` / Vite dev servers strictly to `127.0.0.1` by default, removed wildcard CORS, added `SameOriginWritesMiddleware` (`src/gridforge/web/access.py`) protecting all non-GET/HEAD/OPTIONS routes (`403` on cross-origin writes) plus `TrustedHostMiddleware` (`GRIDFORGE_ALLOWED_HOSTS`, `FORWARDED_ALLOW_IPS`), and pinned `starlette>=1.9` for IPv6 `[::1]` and mixed-case `Host` support.
- **Strict Air-Gapped Partial Pack Handling (PR `#2`):** Offline renders/exports using `--pack` / `--pack-id` now fail cleanly before any online fetch when a partial pack omits DEM or OSM PBF (while allowing completion via `--offline-dem` / `--offline-pbf`), and Studio preserves unavailable or `?pack=` permalink selections without silent fallback.
- **Atomic Offline Device Archive Publication (PR `#3`):** Added `src/gridforge/export/_atomic.py` (`staged_archive`) so Garmin `.kmz` and ATAK `.mbtiles` exports stage atomically on the destination filesystem, properly close SQLite handles, protect `-journal`/`-wal`/`-shm`/`.aux.xml` sidecars, and clean scratch files prior to final atomic rename.
- **Multi-Sheet Atlas Projection & Antimeridian Validation (PR `#4`):** Added shared `compute_atlas_layouts` and antimeridian (`±180°`) extent rejection in `src/gridforge/geo/crs.py`, enforced consistently across `/api/footprint`, `/api/jobs`, and `worker.py`, with digital-only `1×1` footprint preview and stale-footprint clearing on validation errors in Studio.
- **Reproducible Copy CLI & Manifest-ID Pack Lookup (PR `#5`):** Added `resolve_pack_directory` (`--pack-id`) supporting renamed pack folders, restored single-sheet `--elevation-profile` from uploaded `--overlay` routes, switched Studio **Copy CLI** to POSIX single-quoted `docker compose run --rm cli gridforge ...` targeting `/out/` and `/out/inputs/` with unsupported-combo clipboard guards, and added automated `web/tests/copy-cli.test.mjs` (`5/5` passing) + `264` offline pytest checks.

## [1.2.0] - 2026-11-26

Pan-Australia release extending live open-government fire-trail, 4WD/walking track, backcountry hut, campground, and trig station overlays to **all 8 Australian states and territories**, backed by an automatic cross-border router, low-RAM streaming, air-gapped pack bundling, and a redesigned Studio state selector.

### Added

- **Pan-Australia State Trail, Fire-Trail, Hut & Trig Overlays (All 8 States & Territories):**
  - Joining **NSW** (`au-nsw-trails`) and **Victoria** (`au-vic-trails`) with six new live CC BY government overlay sources outputting one unified 2-layer UTM GeoPackage schema (`au_state_trails` + `au_state_pois`):
    - **Tasmania (`au-tas-trails`, `TASTrailsSource`):** *the LIST* (NRE Tas, **CC BY 3.0 AU**) — Overland/walking tracks, fire trails, alpine huts (*Kitchen Hut*, *Rangers Hut*, *Scott-Kilvert Memorial Hut*), and survey control trigs.
    - **Queensland (`au-qld-trails`, `QLDTrailsSource`):** **QSpatial** (DESI / Dept. of Natural Resources & Mines, **CC BY 4.0**) — QPWS Great Walks, park firelines, tourist facilities, lookouts, and survey marks.
    - **ACT (`au-act-trails`, `ACTTrailsSource`):** **ACTmapi / EPSDD** (**CC BY 4.0**) — Namadgi/Tidbinbilla fire trails, heritage walking tracks, huts (*Booth Cottage*), campgrounds, and AHD71 survey marks.
    - **Western Australia (`au-wa-trails`, `WATrailsSource`):** **DBCA AGOL** + **Landgate SLIP** (**CC BY 4.0**) — Tracks & trails, fire breaks, RATIS sites/facilities, fire towers (`DBCA-061`), and geodetic marks (`LGATE-076`); strictly excludes non-commercial `DBCA-027` (CC BY-NC).
    - **South Australia (`au-sa-trails`, `SATrailsSource`):** **DEW / DHUD / Location SA** + **Geoscience Australia Topo250K** (**CC BY 4.0**) — Heysen & Mawson recreation trails streamed via memory-safe GDAL `/vsizip/` + OGR `SetSpatialFilterRect` (`< 20 MB` RSS), DEW fire access tracks, parks facilities JSON, and AHD survey marks.
    - **Northern Territory (`au-nt-trails`, `NTTrailsSource`):** **NTLIS / DEPWS** GeoServer WFS 2.0 + **NTG Road Centrelines** + **GA Topo250K** (**CC BY 4.0**) — 4WD/unformed/Larapinta tracks, place names/lookouts, and AHD survey control marks.
- **Automatic Cross-Border State Router (`--au-overlay au-state-trails`, `AutoStateTrailsSource`):**
  - Automatically resolves all Australian states/territories intersecting the sheet bounding box, merges cross-border features cleanly into one `au_state_trails` + `au_state_pois` GeoPackage, and populates dynamic `resolved_attributions` so map collar footers credit **only** the states that contributed features.
- **Air-Gapped Offline Pack Integration (`gridforge pack build`):**
  - Bundles `au-state-trails` directly into offline Australian regional packs so `gridforge render --pack <dir> --au-overlay au-state-trails` renders 100% air-gapped with zero network calls.
- **MapLibre Print Studio Auto + 8-State Override UI (`web/`):**
  - Added an **Auto (All 8 States)** state trails toggle plus a compact 8-chip state override bar (`NSW`, `VIC`, `TAS`, `QLD`, `WA`, `SA`, `NT`, `ACT`) wired into URL permalinks, **Copy CLI**, and quick-jump presets (with non-wrapping `.gf-section-pill` header badges).
- **Automated Verification (`211+` offline CI tests + `7/7` live Pan-Australia E2E):**
  - Added `tests/unit/test_au_states_east.py`, `tests/unit/test_au_states_west.py`, and `tests/integration/test_v12_pan_australia_trails.py` validating cross-border merging, dynamic attribution filtering, `/vsizip/` low-RAM streaming, offline `--pack` CLI parity, and live 6-state renders (`Cradle Mountain TAS`, `Lamington QLD`, `Namadgi ACT`, `Stirling Range WA`, `Flinders Ranges SA`, `West MacDonnell NT`, and Auto TAS CLI routing).

## [1.1.0] - 2026-11-26

Field navigation, backcountry alpine cartography, and print-verification release adding five high-impact operational tools across the CLI, PyQGIS renderer, and MapLibre Print Studio.

### Added

- **GPX / GeoJSON Auto-Strip-Map Builder & Elevation Profile (`--strip-route`, `--elevation-profile`, `POST /api/strip-plan`):**
  - Upload or pass any `.gpx` or `.geojson` route to automatically plan minimum-count, true-scale UTM strip sheets (`S1..Sn`) with configurable overlap (`--overlap-pct`) covering 100% of the track in one locked UTM projection.
  - Samples route elevations directly from the rendered DEMs and generates a crisp vector **Route Overview & Elevation Profile** cover page with total distance, cumulative ascent/descent, min/max elevation, and strip sheet index table.
- **100 mm Printer Scale Calibration & True-Scale MGRS Romer Page (`--calibration-page`):**
  - Appends a mathematically verified pure-vector calibration sheet featuring exact **`100.00 mm`** horizontal & vertical metric rulers (`0.000 mm` verified error/skew), a **`4.00 in` (`101.60 mm`)** imperial bar, cut-out **MGRS Romer Scale Corners** (`1:25,000`, `1:24,000`, `1:50,000`), and a `360° / 6400 NATO mil` compass protractor ring so users can confirm 100% / Actual Size printing before going into the field.
- **Interactive Waypoint Markup & Tactical Route Card (`--waypoint`, `--route-card`, `POST /api/route-metrics`):**
  - Drop numbered category-coded waypoints (`WP`, `Camp`, `Water`, `Hazard`, `LZ`) directly on the MapLibre Studio map (`⌖ Markup` / `M` hotkey) or pass repeatable `--waypoint` CLI flags, rendered directly onto the map sheet with dashed route legs.
  - Generates a companion **Tactical Route Card** PDF page listing every waypoint's 10-digit MGRS coordinate, lat/lon, elevation, leg distance, cumulative distance, and WMM2025 Grid & Magnetic azimuths in degrees and NATO mils.
- **GDAL Slope-Angle Avalanche Shading (`--slope-shading`):**
  - Computes discrete steepness bands (`>30°` amber/orange/red/purple avalanche terrain in Color; high-contrast tonal bands in B&W) on the bottom raster stack above hillshade so all contours, trails, and 1 km MGRS lines remain 100% crisp vector.
- **NSW & Victoria State Trail, Fire-Trail, Hut & Trig Overlays (`--au-overlay au-nsw-trails`, `--au-overlay au-vic-trails`):**
  - Live CC BY 4.0 integration with **NSW Spatial Services** (`NSW_Transport_Theme`, `SurveyMarkGDA2020`, `NSW_POI`, `NSW_Features_of_Interest_Category`) and **DataVic / DEECA Vicmap** WFS (`tr_road`, `recweb_tracks`, `foi_point`, `recweb_hut`, `recweb_site`, `geomark_point`) bringing high-visibility burnt-orange fire trails, 4WD/walking tracks, alpine huts, campgrounds, and trig stations (`21` SVG icons, `72` native QGIS 3.40 `.qml` styles).
- **Automated Verification (`196+` offline CI tests):** Extended `tests/integration/verify_pdf.py` with `verify_calibration_page()` (`100.00 mm ± 0.15 mm` vector ruler verification) and added `tests/integration/test_v11_usability.py` covering Mt Kosciuszko NSW, Grampians VIC, and Katoomba Bushwalk Strip Atlas E2E renders.

## [1.0.0] - 2026-11-26

First stable public release of **GridForge** — an open-source, self-hostable military-style MGRS topographic map generator, print studio web app, and offline field device exporter.

### Added

#### Phase 1 — Core True-Scale UTM Topographic Engine & Headless PyQGIS Runtime
- Multi-arch (`linux/arm64` + `linux/amd64`) Docker runtime on `debian:trixie-slim` with native headless **PyQGIS 3.40 LTR**, GDAL `3.10`, Osmium, Poppler, and open-source Liberation/DejaVu/Noto fonts.
- Exact local **UTM (`EPSG:326xx`/`327xx`)** sheet layout math across `1:15,000`, `1:24,000`, `1:25,000`, `1:48,000`, and `1:50,000` on `A4`, `Letter`, `Legal`, and `A3` (portrait & landscape), with Norway `32V` and Svalbard `31X/33X/35X/37X` MGRS zone exceptions.
- Deterministic MGRS 1 km grid, bold 100 km square boundaries, UTM zone-junction handling, neatline principal-digit ticks, Grid Reference Box SVG, and **NOAA WMM2025** TC 3-25.26 True/Grid/Magnetic North declination diagram SVG.
- Isolated `spawn` PyQGIS subprocess renderer (`rasterizeWholeImage=False`, dual-stack `Multiply` hillshade over landcover with pure crisp vector contours, roads, labels, and grid on top) plus optional layered **GeoPDF** (`--geopdf`).
- Pluggable on-demand cloud-native (**Copernicus GLO-30** COG via `/vsicurl/`, **Overpass** ID-sorted XML with mirror failover & caching) and air-gapped offline (**Local GeoTIFF/VRT** + **`.osm.pbf`**) sources feeding one canonical GeoPackage schema (`config/osmconf.ini`).
- Automated physical-scale PDF verifier (`tests/integration/verify_pdf.py`) asserting MediaBox dimensions and 1 km MGRS grid spacing (`≤ 0.02 mm` observed error, `0.000 mm` skew).

#### Phase 2 — High-Detail Military Cartography, Density Controls & US Regional Sources
- Dedicated **Color** and high-contrast monochrome laser-print **B&W** themes (`62` native QGIS 3.40 `.qml` styles + `18` parameterized SVG symbols): scale-adaptive cased highways with shields & motorway exits, trails, cross-tick railways, runways/helipads, power lines with pylon ticks, pipelines, directional downstream river flow arrows, separate evergreen/deciduous/mixed tree cover, building usage icons, and hilltop/saddle elevation dedup.
- Toggleable density & layer controls (`--contour-density`, `--hillshade-strength`, `--label-density`, `--buildings`, `--trees`, `--power`, `--pipelines`, `--grid`, `--river-arrows`).
- **USGS 3DEP 1 m LiDAR** discovery & `/vsicurl/` streaming with seamless `NAVD88` 10 m fallback (`AutoDEMSource`), plus opt-in US public-domain federal overlays: **FEMA NFHL** flood zones, **USGS NHD** streams, **FAA DOF** obstacle towers, and **NOAA ENC** depth contours/navaids.
- Custom user `.geojson`, `.gpx`, and `.csv` overlay ingestion (`--overlay`).

#### Phase 3 — Offline Field Device Exports
- **ATAK `.mbtiles`** exporter (`gridforge export` / `--mbtiles`): MBTiles 1.3 SQLite archive in `EPSG:3857` TMS with multi-zoom overview pyramids (`PNG` or `JPEG`) from identical collarless 300 DPI cartography.
- **Garmin Custom Maps `.kmz`** exporter (`--kmz`): upright north-up WGS84 `LatLonBox` KML 2.2 archive with strictly baseline non-progressive SOF0 JPEGs (`≤ 1024×1024 px`, `≤ 3 MB`) auto-scaled to respect `standard` (100-tile) and `modern` (500-tile) device profiles.

#### Phase 4 — Australia-Wide Support, Air-Gapped Packs & Self-Hosted Web Print Studio
- **Australia-Wide Cloud-Native DEM (`au-elvis`, `AHD`, UTM `49S`–`56S`):** Geoscience Australia National 5 m LiDAR (2025) with shoreline cliff-ratio discrimination and seamless fallback to Digital Earth Australia AWS S3 GA SRTM 1-second COG (`AHD`).
- **4 National CC BY 4.0 Australian Overlays (`--au-overlay`):** GA National Surface Hydrology (`au-hydro`, including Outback bores/springs/waterholes/dams), DCCEEW CAPAD 2025 Protected Areas (`au-parks`), GA Electricity & Pipelines (`au-infra`), and GA Airfields, Runways & Outback Homesteads (`au-aviation`).
- **Memory-Safe Air-Gapped Pack Builder (`gridforge pack`):** Streams compact offline packs disk-to-disk at `< 300 MB` RAM across all 8 Australian states/territories, 7 curated tactical regions, or custom `w,s,e,n` bboxes, rendered 100% offline via `--pack`.
- **Self-Hosted Print Studio Web App (`docker compose up`):** Zero-Redis FastAPI + SQLite WAL async job queue (`web` `~64 MiB` idle, `worker` `~60 MiB` idle, `~395 MB` typical render peak RSS) with a tactical dark-mode **TypeScript + Vite + MapLibre GL JS** UI featuring live tilted UTM footprints, 1 km MGRS grid overlays, **Place Name Search** (Nominatim proxy), **Multi-Sheet Edge-Matched UTM Atlas PDFs** (`1×1`–`3×3`), **NSW & Victoria outdoor Quick-Jump Presets**, shareable URL permalinks, **Copy CLI**, drag-and-drop overlay uploads, and inline PDF preview.
- Showcase map gallery (`docs/images/`), Blue Mountains sample route overlay (`examples/katoomba_route.geojson`), runnable CLI cookbook (`examples/render-examples.sh`), and `171+` automated offline unit & integration tests.
