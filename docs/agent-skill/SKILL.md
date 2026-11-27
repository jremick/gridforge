---
name: gridforge
description: >-
  Plan, analyze, and render true-scale local UTM military/backcountry MGRS topographic maps,
  compute WMM2025 magnetic declination and G-M angles, run curved-Earth 1st Fresnel RF link
  profiles and GDAL terrain viewsheds, plan GPX/GeoJSON strip atlases, and export offline
  ATAK/Garmin/OsmAnd field bundles using GridForge MCP tools, HTTP API, or CLI --spec-file.
---

# GridForge Agent Skill

Use this skill whenever the user asks about MGRS coordinates, UTM zones, magnetic declination / G-M angles, line-of-sight RF / LoRa / Meshtastic links, GDAL viewsheds, topographic print sheets, strip atlases, or offline ATAK / Garmin / OsmAnd / GeoPackage field maps.

## 1. Choose the Best Execution Path

1. **GridForge MCP tools (preferred when connected):**
   Call MCP tools directly — supporting hosts render interactive maps, RF Fresnel charts, and printable sheet viewers inline, while all hosts receive PNG visual cards and clickable links.
2. **GridForge HTTP API (REST / OpenAPI):**
   Discover server options via `GET /api/capabilities`, run fast calculations (`GET /api/geocode`, `POST /api/footprint`, `POST /api/rf-link-profile`, `POST /api/viewshed-preview`, `POST /api/route-metrics`, `POST /api/ruler-metrics`, `POST /api/strip-plan`), and submit synchronous renders via `POST /api/jobs?wait=true&timeout_s=120`.
3. **GridForge Docker CLI (`ghcr.io/jremick/gridforge:latest`):**
   Use `gridforge inspect --json` for instant telemetry and `gridforge render --spec-file - --output /out/map.pdf --json` for structured headless rendering.

## 2. Core Cartographic Guardrails

- **True-Scale Local UTM Only:** GridForge projects every sheet into its native local UTM zone (`EPSG:326xx` / `EPSG:327xx`). Valid latitude bounds are `[-80.0, 84.0]`. Never suggest Web Mercator for printed sheets.
- **Allowed Scales:** `10000`, `12500`, `15000`, `24000`, `25000`, `48000`, `50000`.
  - Use `1:10,000` or `1:12,500` when 1 m LiDAR is available and the user wants dense `2 m` / `5 ft` contours over a tight objective.
  - Use `1:24,000` (US `letter`, `imperial`) or `1:25,000` (AU/NZ/CA/Global `a4`, `metric`) for standard backcountry navigation.
  - Use `1:50,000` for wide-area patrol / multi-day planning or multi-sheet `2×2`–`3×3` atlases.
- **Paper Sizes & Orientations:** `a4`, `letter`, `legal`, `a3`; `portrait` or `landscape`.
- **DPI:** `300` (default crisp print), `450`, or `600` (high-detail 1 m LiDAR / fine hillshade).
- **Atlas & Antimeridian Rules:** Multi-sheet grids (`grid_cols` × `grid_rows`, each `1..3`) cannot cross UTM zone boundaries, the equator, or the `±180°` antimeridian. For long linear routes, use `plan_strip_atlas` (`POST /api/strip-plan`) instead of a large rectangular grid.

## 3. Select Regional High-Res Open Data

Default to `dem_source: "auto"` (5-way `AutoDEMSource` routes US → Canada → Australia → New Zealand → Global automatically), or set an explicit provider + regional overlays:

| Region | `dem_source` | Vertical Datum | Resolution | Recommended Regional Overlays | Typical Paper & Units |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **United States** | `usgs3dep` | `NAVD88` | 1 m LiDAR / 10 m | `us_overlays`: `us-trails`, `us-public-lands` (optional: `fema`, `nhd`, `towers`, `enc`) | `letter`, `imperial`, `1:24000` |
| **Canada** | `ca-hrdem` | `CGVD2013` | 1 m/2 m HRDEM → 30 m MRDEM | `ca_overlays`: `ca-canvec-trails` | `letter` or `a4`, `metric`, `1:25000` |
| **Australia** | `au-elvis` | `AHD` | QLD 1 m/2 m → GA 5 m → SRTM 1 s | `au_overlays`: `au-state-trails` (auto all 8 states/territories, or `au-nsw-trails`, `au-vic-trails`, `au-tas-trails`, `au-qld-trails`, `au-act-trails`, `au-wa-trails`, `au-sa-trails`, `au-nt-trails`; optional: `au-hydro`, `au-parks`, `au-infra`, `au-aviation`) | `a4`, `metric`, `1:25000` |
| **New Zealand** | `nz-linz` | `NZVD2016` | AWS 1 m LiDAR → 8 m | `nz_overlays`: `nz-doc-trails` (Great Walks, huts, campsites, LINZ peaks) | `a4`, `metric`, `1:25000` |
| **Global / Other** | `glo30` | `EGM2008` | Copernicus 30 m | OpenStreetMap (automatic) | `a4`, `metric`, `1:25000` |

**Offline / Air-Gapped Packs:** Call `list_packs` (`GET /api/packs`) first when the user wants offline operation or mentions an installed pack. Pass `pack_id` to guarantee 100% air-gapped rendering without silent online fallback.

## 4. Select Cartographic Theme (`color_mode`)

- `color` — Daytime backcountry & tactical topographic map (default).
- `bw` — Black-and-white laser printer edition (diagonal forest hatches, stippled scrub, crisp cased roads).
- `night-red` — Red-headlamp safe (`R ≤ 61` on critical contours, trails, roads, water, grid, viewshed, and tactical layers so nothing washes out under 625–660 nm red light).
- `alpine` — Winter / ski-mountaineering (glacier-blue contours + boosted `30° / 35° / 45°+` avalanche slope shading; also set `carto.show_slope_shading: true`).

## 5. Workflows & Answers

### A. Navigation, MGRS & Declination Briefings
1. Call `answer_location_briefing` (or `geocode_place` → `preview_footprint` / `gridforge inspect --json`).
2. Always report clearly:
   - **10 m MGRS** & **100 km Grid Square(s)**
   - **UTM Zone & EPSG** (e.g. `55S / EPSG:32755`, GZD `55H`)
   - **WMM2025 Magnetic Declination** (+ annual change `°/yr`), **Grid Convergence**, and **G-M Angle** (degrees + NATO mils)
   - **True-Scale Ground Footprint** (`width × height km`) for the requested scale/paper
   - Recommended regional DEM, vertical datum, and trail/land overlays.

### B. RF Links (LoRa / Meshtastic / UHF / VHF) & Terrain Viewsheds
1. Pick the matching band if the user specifies a radio type:
   - **LoRa / Meshtastic:** `915` MHz (US/AU/NZ) or `868` MHz (EU)
   - **AU UHF CB:** `477` MHz · **US GMRS:** `462` MHz · **VHF 2m:** `146` MHz · **Wi-Fi / HaLow:** `2400` / `5800` MHz
2. Call `analyze_rf_link` (`POST /api/rf-link-profile`) with realistic antenna heights (`from_height_m`, `to_height_m`: e.g. `2.0 m` handheld, `10.0 m` mast/relay).
3. Report verdict (`CLEAR`, `MARGINAL`, or `OBSTRUCTED`), path distance (km), FSPL (dB), minimum 1st Fresnel clearance (m & %), and True / Grid / Magnetic bearings + NATO mils.
4. For area coverage from a summit or relay, call `compute_viewshed` (`POST /api/viewshed-preview`, `max_distance_m` up to `25000`).
5. Offer to render a map with `viewsheds`, `rf_links`, and `include_rf_link_card: true` so the user gets a printable 2-page map + pure-vector **RF Link Card PDF**.

### C. Printable Maps, Strip Atlases & Offline Field Bundles
1. Enable helpful field supplements when appropriate:
   - `include_calibration_page: true` — exact 100.00 mm printer scale check + cut-out MGRS Romers.
   - `include_route_card: true` — when `waypoints` are included (`waypoint`, `camp`, `water`, `hazard`, `lz`).
   - `include_rf_link_card: true` — when `rf_links` are included.
   - `include_atlas_index: true`, `include_elevation_profile: true`, `show_fold_marks: true` — for multi-sheet or strip atlases.
2. Enable offline device exports in `outputs` when asked:
   - `pdf` (default `true`), `geopdf`, `mbtiles` (ATAK), `kmz` (Garmin Custom Maps), `gpkg_tiles` (OGC GeoPackage), `osmand_sqlitedb` (OsmAnd), or `field_bundle_zip` (all-in-one `.zip` + GPX 1.1).
3. Call `render_map` (or `POST /api/jobs?wait=true&timeout_s=120`). For multi-page outputs, use `get_job_page_preview` if you need to visually verify page 2+.

### D. Declarative `--spec-file` JSON Template (CLI or `POST /api/jobs?wait=true`)

```json
{
  "center": "-36.456,148.263",
  "title": "MT KOSCIUSZKO — RF & TACTICAL",
  "scale": 25000,
  "paper": "a4",
  "orientation": "portrait",
  "color_mode": "color",
  "units": "metric",
  "dpi": 300,
  "dem_source": "au-elvis",
  "au_overlays": ["au-state-trails"],
  "viewsheds": [
    {
      "label": "Kosciuszko Relay",
      "lat": -36.4559,
      "lon": 148.2636,
      "observer_height_m": 10.0,
      "target_height_m": 1.5,
      "max_distance_m": 8000.0
    }
  ],
  "rf_links": [
    {
      "from_label": "Kosciuszko Relay",
      "to_label": "Seamans Hut",
      "from_lat": -36.4559,
      "from_lon": 148.2636,
      "to_lat": -36.4535,
      "to_lon": 148.2877,
      "freq_mhz": 915.0,
      "from_height_m": 10.0,
      "to_height_m": 5.0
    }
  ],
  "include_rf_link_card": true,
  "outputs": {
    "pdf": true,
    "field_bundle_zip": true
  }
}
```

Run via Docker CLI:

```bash
docker run -i --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --spec-file - --output /out/kosciuszko.pdf --json < spec.json
```

## 6. Delivering Results to the User

Whenever you complete a render or plan:
1. Present clickable **signed artifact download links** (`signed_download_url`, noting the default 1-hour expiry) for the PDF and any device exports.
2. Provide a clickable **Studio share link** (`create_share_link` → `/?share=<token>`) or Studio deep link so the user can open, tweak, and re-render in the browser.
3. Summarize key field telemetry: center MGRS, UTM zone, WMM2025 G-M angle, scale, paper/DPI, theme, and DEM/overlay attribution.
