# GridForge Plugin for Google Antigravity

Install GridForge directly into **Google Antigravity** to unlock:

- **Interactive Inline MCP Apps (`ui://gridforge/app.html`, SEP-1865):** Zoomable/pannable UTM footprint & 1 km MGRS map canvas, SVG WMM2025 declination compass, interactive hover-scrub curved-Earth 1st Fresnel RF link chart, terrain viewsheds, route/strip atlas planners, and a live-polling multi-page printable PDF sheet viewer.
- **Side Pane (`aux_pane`) Entrypoint — `GridForge Studio`:** Open from the side pane `+` menu or let the agent drive it live.
- **Sidebar (`global`) Entrypoint — `GridForge Maps`:** Full-page map planning workspace in the left sidebar.

---

## 30-Second Install

```bash
cp -r plugins/antigravity ~/.gemini/config/plugins/gridforge
```

*(Optional)* Bundle the GridForge cartography skill inside the plugin so the agent automatically chooses regional LiDAR DEMs, overlays, and themes:

```bash
mkdir -p ~/.gemini/config/plugins/gridforge/skills/gridforge
cp docs/agent-skill/SKILL.md ~/.gemini/config/plugins/gridforge/skills/gridforge/SKILL.md
```

Antigravity discovers `~/.gemini/config/plugins/gridforge` automatically — no restart required. Open **Customizations** in Antigravity to verify the **GridForge** plugin is active.

---

## Connecting to Local Studio or a Remote Hosted Instance

The default [`mcp_config.json`](mcp_config.json) runs `ghcr.io/jremick/gridforge:latest gridforge mcp` over local Docker `stdio` using `${PLUGIN_DATA}` for persistent jobs, cache, and offline packs. That works immediately for all instant tools (navigation briefings, geocoding, UTM footprints, WMM2025 declination, RF Fresnel links, viewsheds, route metrics, and strip planning).

To also run full **300–600 DPI PDF & device export renders (`render_map`)** inside Antigravity, connect the plugin to a running GridForge worker:

### Option A — Local Self-Hosted Compose Studio (`http://127.0.0.1:8080`)

Start the local stack (`docker compose -f docker-compose.ghcr.yml up -d`) and point `~/.gemini/config/plugins/gridforge/mcp_config.json` directly at `/mcp`:

```json
{
  "mcpServers": {
    "gridforge": {
      "serverUrl": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

### Option B — Remote Hosted GridForge / GridMaps Instance (`https://<your-domain>`)

Mint a Bearer Personal Access Token (`gf_...`) via `POST /api/auth/api-keys` (see [`deploy/hosted-mode.md`](../../deploy/hosted-mode.md)), then configure Streamable HTTP or Docker `stdio` proxy:

```json
{
  "mcpServers": {
    "gridforge": {
      "serverUrl": "https://<your-domain>/mcp",
      "headers": {
        "Authorization": "Bearer gf_<your-api-key>"
      }
    }
  }
}
```

Or via Docker `stdio` forwarding to a remote URL:

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
        "https://<your-domain>",
        "--public-url",
        "https://<your-domain>"
      ],
      "env": {
        "GRIDFORGE_API_KEY": "gf_<your-api-key>"
      }
    }
  }
}
```

See [`docs/ai-agents.md`](../../docs/ai-agents.md) for the complete 14-tool MCP reference, prompts, resources, and other AI client guides.
