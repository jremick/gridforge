# Changelog

All notable changes to **GridForge** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
