# GridForge

Open-source, self-hostable military-style MGRS topographic map generator built on headless **PyQGIS 3.40 LTR**, **GDAL**, and **Python 3.11+**.

GridForge generates physically accurate, print-ready 300 DPI topographic PDF sheets projected in true local **UTM (`EPSG:326xx`/`327xx`)** — never Web Mercator — so MGRS 1 km grid squares are straight, orthogonal, and true to scale with a map protractor.

## Features

- **True-Scale Local UTM Print Sheets:** Supports `1:15,000`, `1:24,000`, `1:25,000`, `1:48,000`, and `1:50,000` on `A4`, `Letter`, `Legal`, and `A3` in portrait or landscape orientation.
- **MGRS Grid & Marginalia:** Full 1 km grid, bold 100 km square boundaries, UTM zone-junction handling (including Norway `32V` and Svalbard `31X/33X/35X/37X` exceptions), military neatline principal-digit ticks, and Grid Reference Box.
- **WMM2025 Declination Diagram:** Computes True North, Grid North (exact `pyproj` meridian convergence), and Magnetic North (NOAA WMM2025 via `pygeomag`, with custom `.COF` override support) plus TC 3-25.26 G-M conversion notes.
- **Dedicated Color & B&W Themes:** Crisp military topo color styling and a dedicated monochrome laser-print B&W theme (not post-process grayscale desaturation), plus optional layered GeoPDF output (`--geopdf`).
- **Cloud-Native & Air-Gapped Parity:** Streams Copernicus GLO-30 DEM COGs via GDAL `/vsicurl/` and fetches OpenStreetMap vectors on demand via Overpass, or runs completely offline against local GeoTIFF DEMs and `.osm.pbf` extracts through the exact same normalized GeoPackage pipeline.
- **Multi-Arch Docker Runtime:** Native `linux/arm64` (Apple Silicon) and `linux/amd64` container image built on `debian:trixie-slim` with `python3-qgis` 3.40 LTR.

## Quick Start (Docker)

Build the multi-arch development image:

```bash
docker build -t gridforge:dev .
```

Inspect UTM zone, footprint bounds, and WMM2025 declination for a location:

```bash
docker run --rm -v "$(pwd):/app" gridforge:dev \
  gridforge inspect --center "-33.732,150.312" --scale 25000 --paper a4
```

Render a 1:25,000 A4 color topographic PDF (Blue Mountains, NSW):

```bash
mkdir -p output
docker run --rm -v "$(pwd):/app" -v "$(pwd)/output:/output" gridforge:dev \
  gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --orientation portrait \
    --mode color \
    --units metric \
    --title "BLUE MOUNTAINS — KATOOMBA" \
    --output /output/blue_mountains_a4_25k_color.pdf
```

Render a monochrome laser-print B&W sheet using offline local data:

```bash
docker run --rm -v "$(pwd):/app" -v "$(pwd)/output:/output" gridforge:dev \
  gridforge render \
    --center "56H KH 5100 6400" \
    --scale 25000 \
    --paper a4 \
    --mode bw \
    --offline-dem /data/dem.tif \
    --offline-pbf /data/region.osm.pbf \
    --output /output/offline_bw.pdf
```

## Running Tests

```bash
# Unit tests
docker run --rm -v "$(pwd):/app" gridforge:dev pytest tests/unit

# Offline end-to-end render tests
docker run --rm -v "$(pwd):/app" gridforge:dev pytest tests/integration -m "not live"
```

## Architecture

```text
src/gridforge/
├── config.py              # Pydantic v2 SheetSpec, SheetLayout, JobWorkspace contracts
├── cli.py                 # Typer CLI (`gridforge render`, `gridforge inspect`)
├── geo/                   # UTM/MGRS CRS, 1 km/100 km grid generator, WMM2025 declination SVGs
├── sources/               # Pluggable ElevationSource, VectorSource, OverlaySource implementations
├── pipeline/              # OSM -> GeoPackage normalization, GDAL terrain/hillshade/contours, runner
└── render/                # Isolated PyQGIS subprocess worker, styling, and QgsPrintLayout collar
config/
└── osmconf.ini            # Shared GDAL OSM driver schema for online & offline parity
assets/styles/
├── color/                 # Color military topographic theme styles
└── bw/                    # Dedicated B&W monochrome laser-print styles
```

## Data Attributions & License

- **GridForge Source Code:** Licensed under the **GNU General Public License v3.0 or later (GPL-3.0-or-later)**. See [LICENSE](LICENSE).
- **OpenStreetMap Data:** Map data © OpenStreetMap contributors (ODbL) — <https://www.openstreetmap.org/copyright>.
- **Copernicus DEM:** Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
- **Magnetic Model:** NOAA/NCEI World Magnetic Model (WMM2025), US Public Domain.
