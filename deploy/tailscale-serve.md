# Deploying GridForge Privately with Tailscale Serve

[Tailscale Serve](https://tailscale.com/kb/1312/serve) is the fastest, zero-port-forwarding way to give your phones, laptops, and field tablets encrypted HTTPS access to your self-hosted **GridForge Print Studio** over your private Tailnet.

## 1. Start GridForge Locally (Bound to Loopback)

`docker-compose.ghcr.yml` binds strictly to `127.0.0.1:8080` by default so it is never exposed to the local LAN without TLS. Set `GRIDFORGE_ALLOWED_HOSTS` to your Tailnet MagicDNS hostname (and `FORWARDED_ALLOW_IPS=127.0.0.1` so `X-Forwarded-Proto: https` from `tailscale serve` is trusted by GridForge's same-origin middleware):

```bash
mkdir -p packs
GRIDFORGE_ALLOWED_HOSTS="<your-host>.<your-tailnet>.ts.net" \
FORWARDED_ALLOW_IPS="127.0.0.1" \
docker compose -f docker-compose.ghcr.yml up -d
```

Verify the stack is healthy:

```bash
curl -fsS http://127.0.0.1:8080/api/health
```

Expected response:

```json
{"status":"ok","queue_depth":0,"worker_alive":true,"memory_budget_mb":3072,...}
```

## 2. Publish Over Tailnet HTTPS

Ensure **MagicDNS** and **HTTPS Certificates** are enabled in your Tailscale admin console, then run:

```bash
tailscale serve --bg http://127.0.0.1:8080
```

Check your active Tailnet HTTPS URL:

```bash
tailscale serve status
```

Open `https://<your-host>.<your-tailnet>.ts.net` from any device on your Tailnet.

## 3. Running Air-Gapped / Offline Basemap on a Field Laptop

When operating without internet connectivity (e.g., SAR basecamp or overland vehicle homelab):

1. Build or copy regional offline packs into `./packs/` (or use the Studio **Manage Packs** modal before leaving connectivity).
2. Optional: place a raster tile `.mbtiles` archive at `./basemap.mbtiles`, uncomment `GRIDFORGE_BASEMAP_MBTILES=/data/basemap.mbtiles` and the `./basemap.mbtiles:/data/basemap.mbtiles:ro` volume mount in `docker-compose.ghcr.yml`, and recreate the `web` service.
3. GridForge Studio will automatically expose an **Offline** basemap switcher option served from `/api/basemap/{z}/{x}/{y}.{format}` with zero external map tile requests.

## 4. Security Note on Tailscale Funnel

> **Caution:** Do **not** use `tailscale funnel` (public internet exposure) without putting an authentication proxy (such as Caddy `basic_auth`, Authelia, or Authentik) in front of GridForge, since the Print Studio triggers CPU/memory-intensive PyQGIS render jobs and Pack builds. Keep it private with `tailscale serve` for personal or team Tailnets.

## 5. Teardown

```bash
tailscale serve --https=443 off
docker compose -f docker-compose.ghcr.yml down
```
