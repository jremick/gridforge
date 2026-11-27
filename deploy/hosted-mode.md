# Public Hosted Multi-Tenant Deployment (`GRIDFORGE_AUTH_MODE=hosted`)

GridForge defaults to a zero-login local-first deployment (`GRIDFORGE_AUTH_MODE=none` on `127.0.0.1:8080`). When running a public hosted instance (such as **[GridMaps](https://gridmaps.net)** on Railway + Cloudflare Free), enable `GRIDFORGE_AUTH_MODE=hosted` to turn on invite-only accounts, multi-tenant data isolation, tiered render caps, bot protection, automatic artifact/cache pruning, and custom instance branding.

---

## Single-Container Architecture (Web + Worker)

The default `Dockerfile` runs `python3 -m gridforge.web.entrypoint`, which supervises both:
- **FastAPI web server** (`uvicorn`, zero `qgis`/`PyQt5` imports, low idle RAM)
- **Single-concurrency PyQGIS worker** (`gridforge.web.worker`, ephemeral spawned subprocesses `< 2 GB` peak RSS)

Both processes share one persistent volume mounted at `/data` (`GRIDFORGE_DATA_DIR=/data/jobs`, `GRIDFORGE_PACKS_DIR=/data/packs`, `GRIDFORGE_CACHE_DIR=/data/cache`).

See [`railway.toml`](../railway.toml) for the Railway build and `/api/health` healthcheck definition.

---

## Core Environment Variables

### 1. Auth Mode, Session Signing & Bootstrap Admin

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_AUTH_MODE` | `none` | Set to `hosted` to enable accounts, roles, quotas, and tenant isolation. |
| `GRIDFORGE_SESSION_SECRET` | *(required in hosted)* | Random `>= 32`-byte HMAC secret for signing session cookies (`openssl rand -hex 32`). |
| `GRIDFORGE_BOOTSTRAP_ADMIN_EMAIL` | *(unset)* | Seeds the initial Admin account on first boot if no Admin exists yet. |
| `GRIDFORGE_BOOTSTRAP_ADMIN_PASSWORD` | *(auto-generated)* | Optional initial Admin password; if omitted, a one-time random password is logged once at startup. |
| `GRIDFORGE_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated public hostnames accepted by `TrustedHostMiddleware` (e.g. `maps.example.com`). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Set to `*` when behind a trusted cloud edge / reverse proxy (Cloudflare + Railway) so HTTPS `X-Forwarded-Proto` and client IP headers are honored. |

### 2. Custom Instance Branding (Optional)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_BRAND_NAME` | `GridForge` | Brand name rendered in HTML `<title>`, topbar header, `GET /api/auth/session`, and PDF map collars. |
| `GRIDFORGE_BRAND_SUBTITLE` | `Studio` | Short badge next to the brand name in the Studio header. |
| `GRIDFORGE_BRAND_TAGLINE` | `Tactical Topographic Print Studio` | Page `<title>` / meta description tagline. |

### 3. Google OAuth & Cloudflare Turnstile (Optional)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_GOOGLE_CLIENT_ID` | *(unset)* | Google OAuth 2.0 Web Client ID for invited Friends/Admins (`<https://your-domain>/api/auth/google/callback`). |
| `GRIDFORGE_GOOGLE_CLIENT_SECRET` | *(unset)* | Google OAuth 2.0 Web Client Secret. |
| `GRIDFORGE_TURNSTILE_SITEKEY` | *(unset)* | Cloudflare Turnstile site key protecting Guest renders and DEM previews. |
| `GRIDFORGE_TURNSTILE_SECRET` | *(unset)* | Cloudflare Turnstile secret key verified server-side. |

### 4. Role Caps, DPI Limits & Quotas

| Variable | Default (`hosted`) | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_GUEST_MAX_GRID_CELLS` | `1` | Maximum atlas sheets per Guest render (`1` = single sheet). |
| `GRIDFORGE_FRIEND_MAX_GRID_CELLS` | `9` | Maximum atlas sheets per invited Friend render (`3×3`). |
| `GRIDFORGE_GUEST_STRIP_ALLOWED` | `false` | Allow/disallow GPX Strip Atlas for Guests. |
| `GRIDFORGE_FRIEND_STRIP_ALLOWED` | `true` | Allow/disallow GPX Strip Atlas for Friends (`GRIDFORGE_FRIEND_MAX_STRIP_SHEETS=12`). |
| `GRIDFORGE_GUEST_ALLOWED_OUTPUTS` | `pdf` | Comma-separated output kinds allowed for Guests (`pdf,geopdf,mbtiles,kmz,gpkg_tiles,osmand_sqlitedb,field_bundle_zip`). |
| `GRIDFORGE_FRIEND_ALLOWED_OUTPUTS` | *(all)* | Comma-separated output kinds allowed for Friends. |
| `GRIDFORGE_GUEST_MAX_DPI` | `300` | Maximum Print PDF and Digital Export DPI for Guests (`72`–`600`). |
| `GRIDFORGE_FRIEND_MAX_DPI` | `600` | Maximum Print PDF and Digital Export DPI for Friends (`72`–`600`). |
| `GRIDFORGE_MAX_UPLOAD_BYTES_GUEST` | `5242880` (`5 MB`) | Max overlay upload size for Guests (`FRIEND=25 MB`, `ADMIN=50 MB`). |
| `GRIDFORGE_GUEST_DAILY_RENDERS` | `2` | Per-guest daily render quota (`FRIEND=20`, `ADMIN=unlimited`). |
| `GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS` | `50` | Global daily cap across all Guest renders; Admin can also pause public renders live via the Admin modal. |
| `GRIDFORGE_GUEST_HOURLY_PREVIEWS` | `5` | Hourly DEM viewshed/ruler preview quota for Guests (`FRIEND=60`). |
| `GRIDFORGE_SHARE_HOURLY_LIMIT` | `30` | Hourly shareable snapshot link creation cap for Guests (`GRIDFORGE_FRIEND_SHARE_HOURLY_LIMIT=120`, `ADMIN=unlimited`). |
| `GRIDFORGE_MAX_QUEUE_DEPTH` | `10` | Maximum queued jobs before returning `503 Service Unavailable`. |

### 5. Storage Retention & LRU Cache Pruning

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIDFORGE_PRUNE_INTERVAL_SECONDS` | `900` (`15 min`) | Background retention & cache pruning cadence in the worker process. |
| `GRIDFORGE_GUEST_TTL_HOURS` | `24` | Delete completed Guest job artifacts after `24` hours. |
| `GRIDFORGE_FRIEND_TTL_HOURS` | `336` (`14 days`) | Delete completed Friend job artifacts after `14` days. |
| `GRIDFORGE_ADMIN_TTL_HOURS` | `720` (`30 days`) | Delete completed Admin job artifacts after `30` days. |
| `GRIDFORGE_GUEST_SHARE_TTL_HOURS` | `168` (`7 days`) | Expiry TTL for Guest-created `?share=` snapshot links (`GRIDFORGE_FRIEND_SHARE_TTL_HOURS=720` / `30 days`; `ADMIN`/local never expire by default). |
| `GRIDFORGE_CACHE_MAX_BYTES` | `2147483648` (`2 GB`) | LRU cap on `/data/cache` (Overpass, DEM clips, overlays) to protect small cloud volumes. |

---

## Railway + Cloudflare Checklist

1. Attach a persistent volume mounted at `/data` (e.g. `5 GB`) to the Railway service.
2. Set `GRIDFORGE_AUTH_MODE=hosted`, `GRIDFORGE_SESSION_SECRET`, `GRIDFORGE_BOOTSTRAP_ADMIN_EMAIL`, `GRIDFORGE_ALLOWED_HOSTS=<your-domain>`, `FORWARDED_ALLOW_IPS=*`, and `GRIDFORGE_CACHE_DIR=/data/cache`.
3. Point your domain DNS at Railway via Cloudflare Proxy (`SSL/TLS: Full (Strict)`), keeping the Cloudflare free tier in front of Railway for HTTPS termination, DDoS mitigation, and optional Turnstile.
4. Sign in with the bootstrap Admin credentials and issue single-use Friend invite links from the Studio **Admin** modal.
