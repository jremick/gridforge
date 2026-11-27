# Public Hosted Multi-Tenant Deployment (`GRIDFORGE_AUTH_MODE=hosted`)

GridForge defaults to a zero-login local-first deployment (`GRIDFORGE_AUTH_MODE=none` on `127.0.0.1:8080`). When running a public hosted instance (for example on Railway + Cloudflare Free), enable `GRIDFORGE_AUTH_MODE=hosted` to turn on invite-only accounts, multi-tenant data isolation, tiered render caps, bot protection, automatic artifact/cache pruning, and custom instance branding.

---

## Single-Container Architecture (Web + Worker)

The default `Dockerfile` runs `python3 -m gridforge.web.entrypoint` as non-root `UID 10001` (automatically fixing mounted volume ownership and dropping privileges if launched with `RAILWAY_RUN_UID=0` on Railway volumes). It supervises both:
- **FastAPI web server** (`uvicorn`, zero `qgis`/`PyQt5` imports, low idle RAM)
- **Single-concurrency PyQGIS worker** (`gridforge.web.worker`, ephemeral spawned subprocesses `< 2 GB` peak RSS, with web secrets stripped from the worker environment)

Both processes share one persistent volume mounted at `/data` (`GRIDFORGE_DATA_DIR=/data/jobs`, `GRIDFORGE_PACKS_DIR=/data/packs`, `GRIDFORGE_CACHE_DIR=/data/cache`, `GRIDFORGE_SAMPLES_DIR=/data/samples`).

See [`railway.toml`](../railway.toml) for the Railway build and `/api/health` healthcheck definition.

---

## Core Environment Variables

### 1. Auth Mode, Session Signing, Proxy Trust & Bootstrap Admin

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_AUTH_MODE` | `none` | Set to `hosted` to enable accounts, roles, quotas, and tenant isolation. |
| `GRIDFORGE_SESSION_SECRET` | *(required in hosted)* | Random `>= 32`-byte HMAC secret for signing session/guest cookies (`openssl rand -hex 32`). |
| `GRIDFORGE_ADMIN_EMAIL` | *(unset)* | Seeds or ensures the initial Admin account on boot. |
| `GRIDFORGE_ADMIN_PASSWORD` | *(unset)* | Initial Admin password (if omitted, Admin signs in via Google OAuth only). |
| `GRIDFORGE_ADMIN_NAME` | `Admin` | Display name for the seeded Admin account. |
| `GRIDFORGE_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated public hostnames accepted by `TrustedHostMiddleware` (e.g. `maps.example.com`). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` (`none`) / private CIDRs (`hosted`) | Trusted reverse-proxy peer CIDRs for `X-Forwarded-Proto` and `X-Forwarded-For` (defaults in hosted mode to `127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1,fc00::/7`). Avoid `*` so attackers cannot spoof leftmost `X-Forwarded-For` entries. |
| `GRIDFORGE_TRUST_CF_CONNECTING_IP` | `false` | Set to `true` when behind Cloudflare Proxy so validated `CF-Connecting-IP` headers partition client quotas and rate limits. Always pair with `GRIDFORGE_ORIGIN_PROXY_SECRET`. |
| `GRIDFORGE_ORIGIN_PROXY_SECRET` | *(unset)* | Shared secret verified against `X-GridForge-Origin-Secret` (customizable via `GRIDFORGE_ORIGIN_PROXY_HEADER`) injected by a Cloudflare Modify Request Header Transform Rule. Blocks direct-to-origin bypasses with `403` while allowing internal `/api/health` probes. |
| `GRIDFORGE_COOKIE_SECURE` | *(auto from HTTPS)* | Set to `true` in hosted HTTPS deployments to force `Secure` on all auth/guest cookies regardless of proxy headers. |
| `GRIDFORGE_SESSION_TTL_HOURS` | `720` | Signed-in session lifetime in hours (`30 days`). |
| `GRIDFORGE_GUEST_COOKIE_TTL_HOURS` | `24` | Anonymous guest cookie lifetime in hours. |
| `GRIDFORGE_ENABLE_DOCS` | `false` (`hosted`) | Keep `/docs`, `/redoc`, `/openapi.json` disabled in public hosted mode unless explicitly set to `true`. |

### 2. Custom Instance Branding (Optional)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_BRAND_NAME` | `GridForge` | Brand name rendered in HTML `<title>`, topbar header, `GET /api/auth/session`, and PDF map collars. |
| `GRIDFORGE_BRAND_SUBTITLE` | `Studio` | Short badge next to the brand name in the Studio header. |
| `GRIDFORGE_BRAND_TAGLINE` | `Tactical Topographic Print Studio` | Page `<title>` / meta description tagline. |

### 3. Google OAuth & Cloudflare Turnstile (Optional)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_GOOGLE_CLIENT_ID` | *(unset)* | Google OAuth 2.0 Web Client ID for invited Friends/Admins (`https://<your-domain>/api/auth/google/callback`). |
| `GRIDFORGE_GOOGLE_CLIENT_SECRET` | *(unset)* | Google OAuth 2.0 Web Client Secret (PKCE `S256` enabled automatically). |
| `GRIDFORGE_GOOGLE_REDIRECT_URI` | *(derived from request)* | Optional explicit Google OAuth callback URL override. |
| `GRIDFORGE_TURNSTILE_SITE_KEY` | *(unset)* | Cloudflare Turnstile site key protecting Guest renders, DEM previews, and shares. |
| `GRIDFORGE_TURNSTILE_SECRET_KEY` | *(unset)* | Cloudflare Turnstile secret key verified server-side (including hostname check against `GRIDFORGE_ALLOWED_HOSTS`). |

### 4. Role Caps, DPI Limits & Quotas

| Variable | Default (`hosted`) | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_GUEST_MAX_GRID_CELLS` | `1` | Maximum atlas sheets per Guest render (`1` = single sheet). |
| `GRIDFORGE_FRIEND_MAX_GRID_CELLS` | `9` | Maximum atlas sheets per invited Friend render (`3×3`). |
| `GRIDFORGE_GUEST_STRIP_ALLOWED` | `false` | Allow/disallow GPX Strip Atlas for Guests (`GRIDFORGE_GUEST_MAX_STRIP_SHEETS=0`). |
| `GRIDFORGE_FRIEND_STRIP_ALLOWED` | `true` | Allow/disallow GPX Strip Atlas for Friends (`GRIDFORGE_FRIEND_MAX_STRIP_SHEETS=12`). |
| `GRIDFORGE_GUEST_ALLOWED_OUTPUTS` | `pdf` | Comma-separated output kinds allowed for Guests (`pdf,geopdf,mbtiles,kmz,gpkg_tiles,osmand_sqlitedb,field_bundle_zip`). |
| `GRIDFORGE_FRIEND_ALLOWED_OUTPUTS` | *(all)* | Comma-separated output kinds allowed for Friends. |
| `GRIDFORGE_GUEST_MAX_DPI` | `300` | Maximum Print PDF and Digital Export DPI for Guests (`72`–`600`). |
| `GRIDFORGE_FRIEND_MAX_DPI` | `600` | Maximum Print PDF and Digital Export DPI for Friends (`GRIDFORGE_ADMIN_MAX_DPI=600`). |
| `GRIDFORGE_MAX_UPLOAD_BYTES_GUEST` | `5242880` (`5 MB`) | Max overlay upload size for Guests (`GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND=25 MB`, `GRIDFORGE_MAX_UPLOAD_BYTES_ADMIN=50 MB`). |
| `GRIDFORGE_GUEST_DAILY_RENDERS` | `2` | Per-guest daily render quota (`GRIDFORGE_FRIEND_DAILY_RENDERS=20`, Admin unlimited). |
| `GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS` | `50` | Global daily cap across all Guest renders; Admin can also pause public renders live via the Admin modal. |
| `GRIDFORGE_GUEST_HOURLY_PREVIEWS` | `5` | Hourly DEM viewshed/ruler preview quota for Guests (`GRIDFORGE_FRIEND_HOURLY_PREVIEWS=60`). |
| `GRIDFORGE_RF_LINK_HOURLY_LIMIT` | `30` | Hourly RF link profile preview quota for Guests (`GRIDFORGE_FRIEND_RF_LINK_HOURLY_LIMIT=120`). |
| `GRIDFORGE_SHARE_HOURLY_LIMIT` | `30` | Hourly shareable snapshot creation cap for Guests (`GRIDFORGE_FRIEND_SHARE_HOURLY_LIMIT=120`). |
| `GRIDFORGE_GEOCODE_HOURLY_LIMIT` | `30` | Per-IP hourly Nominatim geocode cache-miss limit. |
| `GRIDFORGE_AUTH_RATE_LIMIT_PER_MINUTE` | `20` | Per-IP and per-account 1-minute auth rate limit on login and invite redemption. |
| `GRIDFORGE_GUEST_PROJECT_HOURLY_LIMIT` | `20` | Per-guest hourly project creation limit (`GRIDFORGE_MAX_PROJECTS_PER_GUEST=5`). |
| `GRIDFORGE_MAX_GUEST_PROJECTS` | `500` | Global cap on stored anonymous Guest saved projects; oldest/expired Guest projects are evicted automatically on insert so SQLite storage stays bounded (`<= 32 MB` worst case). |
| `GRIDFORGE_MAX_QUEUE_DEPTH` | `10` | Maximum queued jobs before returning `503 Service Unavailable`. |

### 5. Storage Retention & LRU Cache Pruning

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_PRUNE_INTERVAL_SECONDS` | `900` (`15 min`) | Background retention & cache pruning cadence in the worker process. |
| `GRIDFORGE_GUEST_TTL_HOURS` | `24` | Delete completed Guest job artifacts and Guest projects after `24` hours. |
| `GRIDFORGE_FRIEND_TTL_HOURS` | `336` (`14 days`) | Delete completed Friend job artifacts and Friend projects after `14` days. |
| `GRIDFORGE_ADMIN_TTL_HOURS` | `720` (`30 days`) | Delete completed Admin job artifacts after `30` days (`0` disables Admin job expiration). |
| `GRIDFORGE_GUEST_SHARE_TTL_HOURS` | `168` (`7 days`) | Expiry TTL for Guest-created `?share=` snapshot links (`GRIDFORGE_FRIEND_SHARE_TTL_HOURS=720` / `30 days`). |
| `GRIDFORGE_USAGE_RETENTION_DAYS` | `30` | Automatically prune metered `usage_events` rows older than `30` days. |
| `GRIDFORGE_CACHE_MAX_BYTES` | `2147483648` (`2 GB`) | LRU cap on `/data/cache` (Overpass, DEM clips, overlays) to protect small cloud volumes. |
| `OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | Primary OpenStreetMap endpoint that receives map bounding boxes. |
| `OVERPASS_FALLBACK_URLS` | *(empty)* | Comma-separated mirror endpoints authorized to receive the same bounding boxes when the primary endpoint fails. |

### 6. AI Agents, MCP & API Keys

GridForge includes a native Streamable HTTP **MCP server** (`/mcp`), an AI discovery manifest (`/llms.txt`), a machine-readable capability catalog (`/api/capabilities`), complete OpenAPI schemas (`/openapi.json` with `POST /api/jobs?wait=true`), scoped Bearer Personal Access Tokens, and short-lived HMAC-signed artifact/preview URLs.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_API_KEY` | *(unset)* | Optional static Bearer / `X-API-Key` secret for **single-tenant** (`GRIDFORGE_AUTH_MODE=none`) deployments. When unset in `none` mode, zero-login local behavior is preserved; when set, mutating `/api/*` endpoints and `/mcp` require a matching key (`401` otherwise). Ignored in `hosted` mode (which uses per-user PATs below). |
| `GRIDFORGE_SIGNED_URL_TTL_SECONDS` | `3600` (`1 hour`) | Lifetime in seconds for HMAC-signed artifact download links (`/api/jobs/{job_id}/artifacts/{filename}?sig=...`) and PDF page preview URLs (`/api/jobs/{job_id}/pages/{page}.png?sig=...`). |
| `GRIDFORGE_PUBLIC_URL` | *(unset)* | Canonical public base URL (e.g. `https://maps.example.com`). Populates `servers` in `/openapi.json` (for Custom GPT Actions), builds Studio deep links / share links in MCP responses, and prints absolute URLs in `/llms.txt`. |
| `GRIDFORGE_MCP_APP_HTML` | *(bundled)* | Optional path overriding the compiled SEP-1865 MCP App single-file HTML bundle (`src/gridforge/mcp/assets/mcp_app.html`) served at `ui://gridforge/app.html` (useful for live UI development). |

#### Scoped Bearer API Keys (PATs) & Turnstile Bypass (`hosted` Mode)

- **Key Management Endpoints** (authenticated `friend` and `admin` roles only; returns `403` in `GRIDFORGE_AUTH_MODE=none`):
  - `GET /api/auth/api-keys` — lists active and revoked keys for the signed-in user (`key_prefix`, timestamps; never plaintext).
  - `POST /api/auth/api-keys` (`{"name": "Claude Desktop", "expires_days": 90}`) — returns `201 Created` with the plaintext `gf_...` token **once** (only its SHA-256 hash is stored in SQLite).
  - `DELETE /api/auth/api-keys/{key_id}` — immediately revokes a key.
- **Request Auth & Turnstile Bypass:** Callers authenticate via `Authorization: Bearer gf_...` or `X-API-Key: gf_...` (evaluated before session cookies). Valid tokens resolve to the owning `friend` or `admin` account — automatically bypassing guest-only Cloudflare Turnstile challenges while preserving all per-role daily render caps, hourly preview limits, DPI/grid limits, and strict multi-tenant job/project isolation. Invalid, revoked, expired, or disabled-user keys always fail closed with `401 Unauthorized`.
- **AI Discovery & MCP Endpoints:**
  - `POST` / `GET` / `DELETE /mcp` — Streamable HTTP MCP server with interactive SEP-1865 MCP Apps (`ui://gridforge/app.html`), inline PNG visual cards, 14 model-visible tools, 3 app-only tools, 3 resources, and 4 prompts.
  - `GET /llms.txt` — concise plain-text AI index pointing to `/mcp`, `/api/capabilities`, `/openapi.json`, `/docs`, and CLI `--spec-file` / `--json`.
  - `GET /api/jobs/{job_id}/pages` & `GET /api/jobs/{job_id}/pages/{page_num}.png?width=1200&sig=...` — cached PDF page previews accessible via short-lived HMAC signatures without exposing cookies or API keys.

See [`docs/ai-agents.md`](../docs/ai-agents.md) and [`plugins/antigravity/README.md`](../plugins/antigravity/README.md) for ready-to-copy client setups (Claude, Codex, Google Antigravity, ChatGPT, Cursor, VS Code).

---

## Railway + Cloudflare Checklist

1. Attach a persistent volume mounted at `/data` (e.g. `5 GB`) to the Railway service and set `RAILWAY_RUN_UID=0` so `entrypoint.py` can fix volume ownership on boot before dropping privileges to non-root `UID 10001`.
2. In Cloudflare (**Rules → Transform Rules → Modify Request Header**), add a rule on your zone injecting `X-GridForge-Origin-Secret: <random-32-byte-secret>` on all proxied requests.
3. On Railway, set `GRIDFORGE_AUTH_MODE=hosted`, `GRIDFORGE_SESSION_SECRET`, `GRIDFORGE_ADMIN_EMAIL`, `GRIDFORGE_ADMIN_PASSWORD` (optional), `GRIDFORGE_ALLOWED_HOSTS=<your-domain>`, `GRIDFORGE_PUBLIC_URL=https://<your-domain>`, `GRIDFORGE_TRUST_CF_CONNECTING_IP=true`, `GRIDFORGE_ORIGIN_PROXY_SECRET=<same-secret>`, `GRIDFORGE_COOKIE_SECURE=true`, and `GRIDFORGE_CACHE_DIR=/data/cache`. (Set `GRIDFORGE_ENABLE_DOCS=true` only if you want external OpenAPI/ChatGPT Actions tools to fetch `/openapi.json` directly.)
4. Point your domain DNS at Railway via Cloudflare Proxy (`SSL/TLS: Full (Strict)`) and **delete the unused `*.up.railway.app` service domain** once your custom domain is live so all public traffic must traverse Cloudflare.
5. Sign in with the bootstrap Admin credentials and issue single-use Friend invite links from the Studio **Admin** modal (or mint scoped `gf_...` Personal Access Tokens via `POST /api/auth/api-keys` for MCP / AI clients).
