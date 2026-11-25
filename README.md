# GridForge

Open-source, self-hostable military-style MGRS topographic map generator, print studio web app, and offline device exporter built on headless **PyQGIS 3.40 LTR**, **GDAL**, **FastAPI**, and **MapLibre GL JS**.

GridForge generates physically accurate, print-ready 300 DPI topographic PDF sheets projected in true local **UTM (`EPSG:326xx`/`327xx`)** — never Web Mercator — so MGRS 1 km grid squares are straight, orthogonal, and true to scale with a map protractor, and exports the exact same cartography to **ATAK `.mbtiles`** and **Garmin Custom Maps `.kmz`**.

## Features

- **Phase 4 Self-Hosted Print Studio Web App (`docker compose up`):**
  - Full-screen **MapLibre GL JS** tactical map studio on `http://localhost:8080` with live debounced **tilted UTM paper footprint** + **1 km MGRS interior grid** preview, draggable crosshair reticle, zero-API-key basemaps (OpenTopoMap, OSM, Esri World Imagery), live WMM2025 declination/G-M telemetry, drag-and-drop `.geojson`/`.gpx`/`.csv` overlay upload, and offline pack selection.
  - Zero-Redis **SQLite WAL** async job queue with a single-concurrency ephemeral PyQGIS subprocess worker engineered for 16 GB workstations: `web` container (`512m` limit, `~65 MiB` idle, zero `qgis` imports) + `worker` container (`3g` limit, `~60 MiB` idle, `~400 MB` typical render peak RSS).
- **True-Scale Local UTM Print Sheets:** Supports `1:15,000`, `1:24,000`, `1:25,000`, `1:48,000`, and `1:50,000` on `A4`, `Letter`, `Legal`, and `A3` in portrait or landscape orientation, plus optional layered **GeoPDF** (`--geopdf`).
- **MGRS Grid & WMM2025 Declination Collar:** Full 1 km MGRS grid, bold 100 km square boundaries, UTM zone-junction handling (including Norway `32V` and Svalbard `31X/33X/35X/37X` exceptions), military neatline principal-digit ticks, Grid Reference Box, dual Metric/Imperial scale bars, dynamic vertical datum (`AHD` / `NAVD88` / `EGM2008`), and TC 3-25.26 True/Grid/Magnetic North declination diagram powered by NOAA **WMM2025**.
- **Multi-Scale Military Cartography (Dedicated Color & B&W Themes):**
  - Scale-adaptive styling across `1:15k`, `1:24k/25k`, and `1:48k/50k` in both **Color** and dedicated high-contrast laser-print **B&W** themes (never grayscale desaturation).
  - Cased highway/road hierarchy with shields and motorway exits, trails, cross-tick railways, runways/helipads, power lines with pylon ticks, pipelines, directional downstream river flow arrows, separate evergreen/deciduous/mixed tree cover, building usage icons (hospital, fire, police, school, worship, fuel, shelter, comm towers), Outback bores/homesteads/airfields, hilltop/saddle elevations, and multi-directional hillshade + indexed contours.
  - Toggleable density & layer controls (`--contour-density`, `--hillshade-strength`, `--label-density`, `--buildings/--no-buildings`, `--trees/--no-trees`, `--power/--no-power`, `--pipelines/--no-pipelines`, `--grid/--no-grid`, `--river-arrows/--no-river-arrows`).
- **Australia-Wide Cloud-Native Mapping & Air-Gapped Offline Packs (UTM `49S`–`56S`):**
  - **Australian Elevation (`--dem-source au-elvis`, automatic in `--dem-source auto`):** Streams **Geoscience Australia National 5 m LiDAR (2025, `AHD`)** over coastal/urban/regional centres with automatic seamless fallback to **Digital Earth Australia AWS S3 GA SRTM 1-second COG (`AHD`)** across all of mainland Australia & Tasmania — zero multi-TB mirroring and never mixing vertical datums.
  - **National CC BY 4.0 Australian Overlays (`--au-overlay`):** Repeatable opt-in overlays for **GA National Surface Hydrology** (`au-hydro`: perennial/intermittent watercourses, Outback bores, springs, waterholes, tanks, dams), **DCCEEW CAPAD 2025 Protected Areas** (`au-parks`: National Parks, Nature Reserves, IPAs), **GA National Electricity & Pipelines** (`au-infra`), and **GA Airfields, Runways & Outback Homesteads** (`au-aviation`).
  - **Memory-Safe Offline Pack Builder (`gridforge pack`):** Streams compact, self-contained air-gapped packs (`< 300 MB` RAM) for all 8 Australian states/territories (`act`, `tas`, `vic`, `nsw`, `sa`, `qld`, `nt`, `wa`), 7 curated tactical regions (`blue-mountains`, `sydney-basin`, `australian-alps`, `tasmania-wilderness`, `red-centre`, `perth-hills`, `far-north-qld`), or any custom `w,s,e,n` bbox — render offline via a single `--pack <dir>` flag or drop into `./packs/` for the Web App.
- **Global & North America Data Sources:**
  - **Elevation (`--dem-source auto|au-elvis|usgs3dep|glo30`):** Automatically selects **GA 5 m LiDAR / SRTM (`AHD`)** in Australia, **USGS 3DEP 1 m LiDAR / 10 m seamless (`NAVD88`)** in the United States, and **Copernicus GLO-30 (`EGM2008`)** globally.
  - **Optional US Public-Domain Overlays (`--us-overlay`):** **FEMA NFHL** flood zones (`fema`), **USGS NHD** streams (`nhd`), **FAA DOF** obstacle towers (`towers`), and **NOAA ENC** depth contours/navaids (`enc`), plus custom user `.geojson`/`.gpx`/`.csv` overlays (`--overlay`).
- **Offline Device Exports (`gridforge export` / `--mbtiles` / `--kmz`):**
  - **ATAK `.mbtiles`:** MBTiles 1.3 SQLite raster archive in `EPSG:3857` TMS with multi-zoom overview pyramids (`--mbtiles-format png|jpg`).
  - **Garmin Custom Maps `.kmz`:** Upright north-up WGS84 `LatLonBox` KML 2.2 archive with strictly baseline non-progressive JPEGs (`<= 1024×1024 px`, `<= 3 MB`), automatically downscaled to respect Garmin tile caps (`--garmin-profile standard` = 100 tiles, `modern` = 500 tiles, or `--max-tiles N`).
- **Multi-Arch Docker Runtime:** Native `linux/arm64` (Apple Silicon) and `linux/amd64` multi-stage container image (`node:22-slim` + `debian:trixie-slim` with `python3-qgis` 3.40 LTR).

## Quick Start

### 1. Self-Hosted Web Print Studio (`docker compose up`)

Launch the full MapLibre GL JS print studio + async render worker on `http://localhost:8080`:

```bash
docker compose up --build -d
```

- Open **<http://localhost:8080>** to position your sheet on the live tilted UTM / MGRS footprint map, toggle Color/B&W, Australian/US overlays, density controls, upload custom GPX/GeoJSON/CSV overlays, and queue PDF / ATAK `.mbtiles` / Garmin `.kmz` jobs.
- Place any offline regional packs built with `gridforge pack build` inside `./packs/` — they are mounted read-only into `/data/packs` and appear automatically in the Web UI's **Offline Pack** selector.
- Check health and worker heartbeat at `GET http://localhost:8080/api/health`.
- Stop cleanly with `docker compose down`.

### 2. CLI Quick Start (Docker)

Build the multi-arch container image directly (if running standalone CLI commands):

```bash
docker build -t gridforge:dev .
```

Inspect UTM zone, footprint bounds, and WMM2025 declination for a location:

```bash
docker run --rm gridforge:dev \
  gridforge inspect --center "-33.732,150.312" --scale 25000 --paper a4
```

### Australia-Wide Examples (GA 5 m LiDAR / SRTM `AHD` + National CC BY 4.0 Overlays)

Sydney Harbour (GA 5 m LiDAR `AHD`, `1:25,000` A4 Color):

```bash
mkdir -p out
docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render \
    --center "-33.8568,151.2153" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --dem-source au-elvis \
    --title "SYDNEY HARBOUR — NORTH SHORE" \
    --output /out/sydney_25k_a4_color.pdf
```

Uluru / Kata Tjuta Outback NT (UTM `52S`, GA SRTM 1s `AHD` + Outback waterpoints, CAPAD National Park/IPA boundaries, airfields & homesteads):

```bash
docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render \
    --center "-25.3444,131.0369" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --dem-source au-elvis \
    --au-overlay au-hydro \
    --au-overlay au-parks \
    --au-overlay au-aviation \
    --title "ULURU — KATA TJUTA NT" \
    --output /out/uluru_25k_a4_color.pdf
```

Cradle Mountain, Tasmania (B&W laser print + ATAK `.mbtiles` + Garmin `.kmz` in a single pass):

```bash
docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render \
    --center "-41.684,145.951" \
    --scale 25000 \
    --paper a4 \
    --mode bw \
    --dem-source au-elvis \
    --au-overlay au-hydro \
    --au-overlay au-parks \
    --title "CRADLE MOUNTAIN — TASMANIA" \
    --output /out/cradle_mountain_25k_a4_bw.pdf \
    --mbtiles /out/cradle_mountain_25k.mbtiles \
    --kmz /out/cradle_mountain_25k.kmz
```

### Air-Gapped Australia Offline Packs (`gridforge pack`)

List curated Australian state/territory and tactical region presets:

```bash
docker run --rm gridforge:dev gridforge pack list-regions
```

Build a self-contained offline pack (streams OSM, DEM, and national AU overlays disk-to-disk at `< 300 MB` RAM):

```bash
mkdir -p packs
docker run --rm -v "$(pwd)/packs:/data/packs" gridforge:dev \
  gridforge pack build --region blue-mountains --output /data/packs/blue-mountains
```

Inspect a pack and render 100% air-gapped with zero network calls:

```bash
docker run --rm -v "$(pwd)/packs:/data/packs:ro" -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --pack /data/packs/blue-mountains \
    --output /out/offline_pack_sheet.pdf
```

### US High-Res LiDAR + Federal Public-Domain Overlays

Boulder / Flatirons, Colorado (`1:24,000` Letter Imperial, USGS 3DEP 1 m/10 m `NAVD88` DEM + FEMA flood zones, NHD streams, and FAA obstacle towers):

```bash
docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render \
    --center "40.005,-105.285" \
    --scale 24000 \
    --paper letter \
    --mode color \
    --units imperial \
    --dem-source usgs3dep \
    --us-overlay fema \
    --us-overlay nhd \
    --us-overlay towers \
    --title "BOULDER — FLATIRONS, CO" \
    --output /out/boulder_24k_letter_color.pdf \
    --mbtiles /out/boulder_24k.mbtiles \
    --kmz /out/boulder_24k.kmz
```

### Standalone Offline Device Export (`gridforge export`)

Export ATAK `.mbtiles` and/or Garmin `.kmz` directly without generating a print PDF (supports `--no-grid` when using a device's native MGRS grid overlay):

```bash
docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
  gridforge export \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --format both \
    --mbtiles-format png \
    --garmin-profile standard \
    --no-grid \
    --output /out/katoomba_offline
```

### Installing Exported Archives on Devices

- **ATAK (Android):** Copy `<map>.mbtiles` to `atak/imagery/` on the device (internal storage or SD card) and select it in ATAK's **Maps / Imagery** selector. Choose `--mbtiles-format png` for crisp vector linework/labels or `jpg` for smaller hillshade-heavy archives.
- **Garmin Handhelds:** Copy `<map>.kmz` to `Garmin/CustomMaps/` on the handheld or microSD card and enable **Custom Maps** in the device's map setup menu. Use `--garmin-profile standard` (100-tile cap for classic eTrex/GPSMAP/Oregon units) or `--garmin-profile modern` (500-tile cap for GPSMAP 66/67, Montana 700, Oregon 7xx), or override explicitly with `--max-tiles`.

## Running Tests

```bash
# Offline unit + integration CI gate (no network required)
docker run --rm -v "$(pwd):/app" gridforge:dev pytest tests/unit tests/integration

# Live multi-region Australia & US end-to-end tests
docker run --rm -e GRIDFORGE_RUN_LIVE=1 -v "$(pwd):/app" gridforge:dev pytest tests/integration -m live
```

## Architecture

```text
src/gridforge/
├── config.py              # Pydantic v2 SheetSpec, CartoOptions, PackManifest, ExportResult contracts
├── cli.py                 # Typer CLI (`render`, `export`, `inspect`, `pack`, `serve`)
├── geo/                   # UTM/MGRS CRS, 1 km/100 km grid generator, WMM2025 declination SVGs
├── sources/               # GA 5m/SRTM (AU), USGS 3DEP (US), Copernicus GLO-30, Overpass, PBF, AU/US/user overlays
├── packs/                 # Memory-safe streaming Australia offline pack catalog & builder (< 300 MB RAM)
├── pipeline/              # OSM -> GeoPackage normalization, GDAL terrain/hillshade/contours, job runner
├── render/                # Isolated PyQGIS subprocess worker, Color/B&W styling, QgsPrintLayout collar
├── export/                # Collarless UTM GeoTIFF renderer, ATAK MBTiles 1.3, Garmin Custom Maps KMZ
└── web/                   # FastAPI server (zero qgis imports), SQLite WAL job queue, single-concurrency worker
web/                       # TypeScript + Vite + MapLibre GL JS self-hosted tactical print studio SPA
config/
└── osmconf.ini            # Shared GDAL OSM driver schema for online & air-gapped parity
assets/
├── icons/                 # 18 crisp self-contained military/topo/Outback SVG point & line symbols
└── styles/
    ├── color/             # 31 native QGIS 3.40 Color military topographic .qml styles
    └── bw/                # 31 native QGIS 3.40 dedicated monochrome laser-print .qml styles
```

## Data Attributions & License

- **GridForge Source Code:** Licensed under the **GNU General Public License v3.0 or later (GPL-3.0-or-later)**. See [LICENSE](LICENSE).
- **OpenStreetMap Data:** Map data © OpenStreetMap contributors (ODbL) — <https://www.openstreetmap.org/copyright>.
- **Australian National Datasets (CC BY 4.0):**
  - Elevation, Surface Hydrology, National Infrastructure, and Aviation/Homesteads: © Commonwealth of Australia (Geoscience Australia / Digital Earth Australia).
  - Protected Areas: CAPAD 2025 © Commonwealth of Australia (Department of Climate Change, Energy, the Environment and Water — DCCEEW).
- **Copernicus DEM:** Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
- **US Elevation & Federal Overlays (Public Domain):** U.S. Geological Survey 3DEP & National Hydrography Dataset (NHD), FEMA National Flood Hazard Layer (NFHL), FAA Digital Obstacle File (DOF), and NOAA Electronic Navigational Charts (ENC).
- **Magnetic Model:** NOAA/NCEI World Magnetic Model (WMM2025), US Public Domain.
