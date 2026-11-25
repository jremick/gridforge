# GridForge

Open-source, self-hostable military-style MGRS topographic map generator and offline device exporter built on headless **PyQGIS 3.40 LTR**, **GDAL**, and **Python 3.11+**.

GridForge generates physically accurate, print-ready 300 DPI topographic PDF sheets projected in true local **UTM (`EPSG:326xx`/`327xx`)** — never Web Mercator — so MGRS 1 km grid squares are straight, orthogonal, and true to scale with a map protractor, and exports the exact same cartography to **ATAK `.mbtiles`** and **Garmin Custom Maps `.kmz`**.

## Features

- **True-Scale Local UTM Print Sheets:** Supports `1:15,000`, `1:24,000`, `1:25,000`, `1:48,000`, and `1:50,000` on `A4`, `Letter`, `Legal`, and `A3` in portrait or landscape orientation, plus optional layered **GeoPDF** (`--geopdf`).
- **MGRS Grid & WMM2025 Declination Collar:** Full 1 km MGRS grid, bold 100 km square boundaries, UTM zone-junction handling (including Norway `32V` and Svalbard `31X/33X/35X/37X` exceptions), military neatline principal-digit ticks, Grid Reference Box, dual Metric/Imperial scale bars, dynamic vertical datum (`NAVD88` / `EGM2008`), and TC 3-25.26 True/Grid/Magnetic North declination diagram powered by NOAA **WMM2025**.
- **Phase 2 Multi-Scale Military Cartography (Dedicated Color & B&W Themes):**
  - Scale-adaptive styling across `1:15k`, `1:24k/25k`, and `1:48k/50k` in both **Color** and dedicated high-contrast laser-print **B&W** themes (never grayscale desaturation).
  - Cased highway/road hierarchy with shields and motorway exits, trails, cross-tick railways, runways/helipads, power lines with pylon ticks, pipelines, directional downstream river flow arrows, separate evergreen/deciduous/mixed tree cover, building usage icons (hospital, fire, police, school, worship, fuel, shelter, comm towers), hilltop/saddle elevations, and multi-directional hillshade + indexed contours.
  - Fine-grained toggleable density and layer controls (`--contour-density`, `--hillshade-strength`, `--label-density`, `--buildings/--no-buildings`, `--trees/--no-trees`, `--power/--no-power`, `--pipelines/--no-pipelines`, `--grid/--no-grid`, `--river-arrows/--no-river-arrows`).
- **Cloud-Native Global/US Data & Air-Gapped Parity:**
  - **Elevation (`--dem-source auto|glo30|usgs3dep`):** Streams **USGS 3DEP 1 m LiDAR** (with seamless NAVD88 10 m fallback) inside the United States and **Copernicus GLO-30** (`EGM2008`) globally via GDAL `/vsicurl/` — never mixing vertical datums.
  - **Optional US Public-Domain Overlays (`--us-overlay`):** Repeatable opt-in overlays for **FEMA NFHL** flood zones (`fema`), **USGS NHD** streams (`nhd`), **FAA DOF** obstacle towers (`towers`), and **NOAA ENC** depth contours/navaids (`enc`), plus user `.geojson`, `.gpx`, and `.csv` overlays (`--overlay`).
  - **Air-Gapped Offline Mode:** Point `--offline-dem` (local GeoTIFF/VRT or directory) and `--offline-pbf` (local `.osm.pbf` or `.osm`) at local files through the exact same normalized GeoPackage pipeline.
- **Phase 3 Offline Device Exports (`gridforge export` / `--mbtiles` / `--kmz`):**
  - **ATAK `.mbtiles`:** MBTiles 1.3 SQLite raster archive in `EPSG:3857` TMS with multi-zoom overview pyramids (`--mbtiles-format png|jpg`).
  - **Garmin Custom Maps `.kmz`:** Upright north-up WGS84 `LatLonBox` KML 2.2 archive with strictly baseline non-progressive JPEGs (`<= 1024×1024 px`, `<= 3 MB`), automatically downscaled to respect Garmin tile caps (`--garmin-profile standard` = 100 tiles, `modern` = 500 tiles, or `--max-tiles N`).
- **Multi-Arch Docker Runtime:** Native `linux/arm64` (Apple Silicon) and `linux/amd64` container image built on `debian:trixie-slim` with `python3-qgis` 3.40 LTR.

## Quick Start (Docker)

Build the multi-arch container image:

```bash
docker build -t gridforge:dev .
```

Inspect UTM zone, footprint bounds, and WMM2025 declination for a location:

```bash
docker run --rm gridforge:dev \
  gridforge inspect --center "-33.732,150.312" --scale 25000 --paper a4
```

### Render Print PDF + ATAK `.mbtiles` + Garmin `.kmz` (Single Pass)

Blue Mountains, NSW (`1:25,000` A4 Color, Copernicus GLO-30 `EGM2008`, plus matching ATAK & Garmin offline archives):

```bash
mkdir -p out
docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --orientation portrait \
    --mode color \
    --units metric \
    --title "BLUE MOUNTAINS — KATOOMBA" \
    --output /out/katoomba_25k_a4_color.pdf \
    --mbtiles /out/katoomba_25k.mbtiles \
    --kmz /out/katoomba_25k.kmz
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

### Cartographic Density, Layer Toggles & Air-Gapped Offline Mode

Customize map clutter for dense urban or steep alpine sheets, or run completely air-gapped with local files:

```bash
docker run --rm -v "$(pwd)/data:/data:ro" -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render \
    --center "56H KH 5100 6400" \
    --scale 25000 \
    --paper a4 \
    --mode bw \
    --contour-density high \
    --hillshade-strength light \
    --label-density normal \
    --no-pipelines \
    --offline-dem /data/dem.tif \
    --offline-pbf /data/region.osm.pbf \
    --overlay /data/waypoints.gpx \
    --output /out/offline_bw.pdf
```

### Installing Exported Archives on Devices

- **ATAK (Android):** Copy `<map>.mbtiles` to `atak/imagery/` on the device (internal storage or SD card) and select it in ATAK's **Maps / Imagery** selector. Choose `--mbtiles-format png` for crisp vector linework/labels or `jpg` for smaller hillshade-heavy archives.
- **Garmin Handhelds:** Copy `<map>.kmz` to `Garmin/CustomMaps/` on the handheld or microSD card and enable **Custom Maps** in the device's map setup menu. Use `--garmin-profile standard` (100-tile cap for classic eTrex/GPSMAP/Oregon units) or `--garmin-profile modern` (500-tile cap for GPSMAP 66/67, Montana 700, Oregon 7xx), or override explicitly with `--max-tiles`.

## Running Tests

```bash
# Offline unit + integration CI gate (no network required)
docker run --rm -v "$(pwd):/app" gridforge:dev pytest tests/unit tests/integration

# Live end-to-end tests (Copernicus GLO-30, USGS 3DEP, Overpass, US overlays)
docker run --rm -e GRIDFORGE_RUN_LIVE=1 -v "$(pwd):/app" gridforge:dev pytest tests/integration -m live
```

## Architecture

```text
src/gridforge/
├── config.py              # Pydantic v2 SheetSpec, CartoOptions, JobWorkspace, ExportResult contracts
├── cli.py                 # Typer CLI (`gridforge render`, `gridforge export`, `gridforge inspect`)
├── geo/                   # UTM/MGRS CRS, 1 km/100 km grid generator, WMM2025 declination SVGs
├── sources/               # Copernicus GLO-30, USGS 3DEP 1m/10m, AutoDEM, Overpass, PBF, US & user overlays
├── pipeline/              # OSM -> GeoPackage normalization, GDAL terrain/hillshade/contours, runner
├── render/                # Isolated PyQGIS subprocess worker, Color/B&W styling, QgsPrintLayout collar
└── export/                # Collarless UTM GeoTIFF renderer, ATAK MBTiles 1.3, Garmin Custom Maps KMZ
config/
└── osmconf.ini            # Shared GDAL OSM driver schema for online & air-gapped parity
assets/
├── icons/                 # Crisp self-contained military/topo SVG point & line symbols
└── styles/
    ├── color/             # Native QGIS 3.40 Color military topographic .qml styles
    └── bw/                # Native QGIS 3.40 dedicated monochrome laser-print .qml styles
```

## Data Attributions & License

- **GridForge Source Code:** Licensed under the **GNU General Public License v3.0 or later (GPL-3.0-or-later)**. See [LICENSE](LICENSE).
- **OpenStreetMap Data:** Map data © OpenStreetMap contributors (ODbL) — <https://www.openstreetmap.org/copyright>.
- **Copernicus DEM:** Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
- **US Elevation & Federal Overlays (Public Domain):** U.S. Geological Survey 3DEP & National Hydrography Dataset (NHD), FEMA National Flood Hazard Layer (NFHL), FAA Digital Obstacle File (DOF), and NOAA Electronic Navigational Charts (ENC).
- **Magnetic Model:** NOAA/NCEI World Magnetic Model (WMM2025), US Public Domain.
