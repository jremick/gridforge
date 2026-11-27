# Security Policy

## Supported Versions

Security fixes are applied to the latest minor release line of GridForge:

| Version | Supported |
| :--- | :--- |
| `1.7.x` | Yes |
| `< 1.7.0` | No |

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues, pull requests, or discussions.**

Report vulnerabilities privately through **GitHub Security Advisories**:

- **Advisory URL:** <https://github.com/jremick/gridforge/security/advisories/new>

Please include:

- A clear description of the vulnerability and its potential impact.
- Affected GridForge version or commit SHA, component (`web`, `worker`, `mcp`, `cli`, `packs`, `render`, or `sources`), and deployment mode (`none` vs `hosted`).
- Minimal reproduction steps or a proof-of-concept payload.
- Any suggested remediation or workaround, if known.

We aim to acknowledge private reports within **3 business days** and provide an initial triage update within **7 days**. We will coordinate disclosure timing once a fix is ready.

---

## Security Architecture & Deployment Hardening

### 1. Local-First Defaults (`GRIDFORGE_AUTH_MODE=none`)

- **Loopback binding by default:** [`docker-compose.yml`](docker-compose.yml) and [`docker-compose.ghcr.yml`](docker-compose.ghcr.yml) bind Studio to `127.0.0.1:8080`, protected by `TrustedHostMiddleware` and `SameOriginWritesMiddleware` on all mutating endpoints.
- **Private remote access:** Expose a self-hosted instance over SSH port forwarding, [Tailscale Serve](deploy/tailscale-serve.md), or an authenticated TLS reverse proxy ([`deploy/Caddyfile.example`](deploy/Caddyfile.example) / [`deploy/traefik-compose.override.yml`](deploy/traefik-compose.override.yml)).
- **Optional single-tenant API key:** Set `GRIDFORGE_API_KEY` when exposing `/api/*` or `/mcp` programmatically in `none` mode.

### 2. Multi-Tenant Hosted Mode (`GRIDFORGE_AUTH_MODE=hosted`)

See [`deploy/hosted-mode.md`](deploy/hosted-mode.md) for the full production hardening checklist:

- **Role-based tenant isolation & quotas:** Separates `Guest`, `Friend`, and `Admin` roles with per-role daily render caps, DPI/grid caps, upload size limits, rate limiting, and SQLite tenant ownership checks.
- **Origin protection & proxy trust:** Configure `GRIDFORGE_ALLOWED_HOSTS`, narrow `FORWARDED_ALLOW_IPS`, set `GRIDFORGE_COOKIE_SECURE=true`, and pair `GRIDFORGE_TRUST_CF_CONNECTING_IP=true` with `GRIDFORGE_ORIGIN_PROXY_SECRET` so direct-to-origin bypasses fail closed with `403`.
- **Bot protection & OAuth:** Supports server-side Cloudflare Turnstile verification (with hostname enforcement) and Google OAuth 2.0 with PKCE `S256`.
- **Scoped Bearer PATs & signed artifact URLs:** `Friend` and `Admin` accounts can mint revocable `gf_...` Personal Access Tokens (stored only as SHA-256 hashes in SQLite) for `/api/*` and `/mcp`, and artifact/page preview links use short-lived HMAC-SHA256 `?sig=...` signatures (see [`src/gridforge/web/auth.py`](src/gridforge/web/auth.py) and [`docs/ai-agents.md`](docs/ai-agents.md)).

### 3. Container, Upload & PyQGIS Subprocess Isolation

- **Non-root container runtime:** [`Dockerfile`](Dockerfile) runs as dedicated non-root `UID/GID 10001` (`gridforge`), with `cap_drop: [ALL]` and `no-new-privileges:true` in Compose stacks.
- **Process & secret separation:** The FastAPI `web` process never imports `qgis`/`PyQt5`; PyQGIS runs only in ephemeral `spawn` worker subprocesses ([`src/gridforge/render/pyqgis_worker.py`](src/gridforge/render/pyqgis_worker.py)) with web auth secrets stripped from the worker environment.
- **Upload & path guardrails:** Overlay/route uploads in [`src/gridforge/web/app.py`](src/gridforge/web/app.py) enforce per-role size caps, extension allowlists (`.geojson`, `.json`, `.gpx`, `.csv`), filename sanitization, and path-traversal checks; GDAL `/vsicurl/` reads restrict allowed extensions via `CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.vrt`.
