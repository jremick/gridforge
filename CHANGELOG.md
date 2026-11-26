# Changelog

All notable changes to **GridForge** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-26

Pan-Australia release extending live open-government fire-trail, 4WD/walking track, backcountry hut, campground, and trig station overlays to **all 8 Australian states and territories**, backed by an automatic cross-border router, low-RAM streaming, air-gapped pack bundling, and a redesigned Studio state selector.

### Added

- **Pan-Australia State Trail, Fire-Trail, Hut & Trig Overlays (All 8 States & Territories):**
  - Joining **NSW** (`au-nsw-trails`) and **Victoria** (`au-vic-trails`) with six new live CC BY government overlay sources outputting one unified 2-layer UTM GeoPackage schema (`au_state_trails` + `au_state_pois`):
    - **Tasmania (`au-tas-trails`, `TASTrailsSource`):** *the LIST* (NRE Tas, **CC BY 3.0 AU**) — Overland/walking tracks, fire trails, alpine huts (*Kitchen Hut*, *Rangers Hut*, *Scott-Kilvert Memorial Hut*), and survey control trigs.
    - **Queensland (`au-qld-trails`, `QLDTrailsSource`):** **QSpatial** (DESI / Dept. of Natural Resources & Mines, **CC BY 4.0**) — QPWS Great Walks, park firelines, tourist facilities, lookouts, and survey marks.
    - **ACT (`au-act-trails`, `ACTTrailsSource`):** **ACTmapi / EPSDD** (**CC BY 4.0**) — Namadgi/Tidbinbilla fire trails, heritage walking tracks, huts (*Booth Cottage*), campgrounds, and AHD71 survey marks.
    - **Western Australia (`au-wa-trails`, `WATrailsSource`):** **DBCA AGOL** + **Landgate SLIP** (**CC BY 4.0**) — Tracks & trails, fire breaks, RATIS sites/facilities, fire towers (`DBCA-061`), and geodetic marks (`LGATE-076`); strictly excludes non-commercial `DBCA-027` (CC BY-NC).
    - **South Australia (`au-sa-trails`, `SATrailsSource`):** **DEW / DHUD / Location SA** + **Geoscience Australia Topo250K** (**CC BY 4.0**) — Heysen & Mawson recreation trails streamed via memory-safe GDAL `/vsizip/` + OGR `SetSpatialFilterRect` (`< 20 MB` RSS), DEW fire access tracks, parks facilities JSON, and AHD survey marks.
    - **Northern Territory (`au-nt-trails`, `NTTrailsSource`):** **NTLIS / DEPWS** GeoServer WFS 2.0 + **NTG Road Centrelines** + **GA Topo250K** (**CC BY 4.0**) — 4WD/unformed/Larapinta tracks, place names/lookouts, and AHD survey control marks.
- **Automatic Cross-Border State Router (`--au-overlay au-state-trails`, `AutoStateTrailsSource`):**
  - Automatically resolves all Australian states/territories intersecting the sheet bounding box, merges cross-border features cleanly into one `au_state_trails` + `au_state_pois` GeoPackage, and populates dynamic `resolved_attributions` so map collar footers credit **only** the states that contributed features.
- **Air-Gapped Offline Pack Integration (`gridforge pack build`):**
  - Bundles `au-state-trails` directly into offline Australian regional packs so `gridforge render --pack <dir> --au-overlay au-state-trails` renders 100% air-gapped with zero network calls.
- **MapLibre Print Studio Auto + 8-State Override UI (`web/`):**
  - Added an **Auto (All 8 States)** state trails toggle plus a compact 8-chip state override bar (`NSW`, `VIC`, `TAS`, `QLD`, `WA`, `SA`, `NT`, `ACT`) wired into URL permalinks, **Copy CLI**, and quick-jump presets (with non-wrapping `.gf-section-pill` header badges).
- **Automated Verification (`211+` offline CI tests + `7/7` live Pan-Australia E2E):**
  - Added `tests/unit/test_au_states_east.py`, `tests/unit/test_au_states_west.py`, and `tests/integration/test_v12_pan_australia_trails.py` validating cross-border merging, dynamic attribution filtering, `/vsizip/` low-RAM streaming, offline `--pack` CLI parity, and live 6-state renders (`Cradle Mountain TAS`, `Lamington QLD`, `Namadgi ACT`, `Stirling Range WA`, `Flinders Ranges SA`, `West MacDonnell NT`, and Auto TAS CLI routing).

## [1.1.0] - 2026-11-26

Field navigation, backcountry alpine cartography, and print-verification release adding five high-impact operational tools across the CLI, PyQGIS renderer, and MapLibre Print Studio.

### Added

- **GPX / GeoJSON Auto-Strip-Map Builder & Elevation Profile (`--strip-route`, `--elevation-profile`, `POST /api/strip-plan`):**
  - Upload or pass any `.gpx` or `.geojson` route to automatically plan minimum-count, true-scale UTM strip sheets (`S1..Sn`) with configurable overlap (`--overlap-pct`) covering 100% of the track in one locked UTM projection.
  - Samples route elevations directly from the rendered DEMs and generates a crisp vector **Route Overview & Elevation Profile** cover page with total distance, cumulative ascent/descent, min/max elevation, and strip sheet index table.
- **100 mm Printer Scale Calibration & True-Scale MGRS Romer Page (`--calibration-page`):**
  - Appends a mathematically verified pure-vector calibration sheet featuring exact **`100.00 mm`** horizontal & vertical metric rulers (`0.000 mm` verified error/skew), a **`4.00 in` (`101.60 mm`)** imperial bar, cut-out **MGRS Romer Scale Corners** (`1:25,000`, `1:24,000`, `1:50,000`), and a `360° / 6400 NATO mil` compass protractor ring so users can confirm 100% / Actual Size printing before going into the field.
- **Interactive Waypoint Markup & Tactical Route Card (`--waypoint`, `--route-card`, `POST /api/route-metrics`):**
  - Drop numbered category-coded waypoints (`WP`, `Camp`, `Water`, `Hazard`, `LZ`) directly on the MapLibre Studio map (`⌖ Markup` / `M` hotkey) or pass repeatable `--waypoint` CLI flags, rendered directly onto the map sheet with dashed route legs.
  - Generates a companion **Tactical Route Card** PDF page listing every waypoint's 10-digit MGRS coordinate, lat/lon, elevation, leg distance, cumulative distance, and WMM2025 Grid & Magnetic azimuths in degrees and NATO mils.
- **GDAL Slope-Angle Avalanche Shading (`--slope-shading`):**
  - Computes discrete steepness bands (`>30°` amber/orange/red/purple avalanche terrain in Color; high-contrast tonal bands in B&W) on the bottom raster stack above hillshade so all contours, trails, and 1 km MGRS lines remain 100% crisp vector.
- **NSW & Victoria State Trail, Fire-Trail, Hut & Trig Overlays (`--au-overlay au-nsw-trails`, `--au-overlay au-vic-trails`):**
  - Live CC BY 4.0 integration with **NSW Spatial Services** (`NSW_Transport_Theme`, `SurveyMarkGDA2020`, `NSW_POI`, `NSW_Features_of_Interest_Category`) and **DataVic / DEECA Vicmap** WFS (`tr_road`, `recweb_tracks`, `foi_point`, `recweb_hut`, `recweb_site`, `geomark_point`) bringing high-visibility burnt-orange fire trails, 4WD/walking tracks, alpine huts, campgrounds, and trig stations (`21` SVG icons, `72` native QGIS 3.40 `.qml` styles).
- **Automated Verification (`196+` offline CI tests):** Extended `tests/integration/verify_pdf.py` with `verify_calibration_page()` (`100.00 mm ± 0.15 mm` vector ruler verification) and added `tests/integration/test_v11_usability.py` covering Mt Kosciuszko NSW, Grampians VIC, and Katoomba Bushwalk Strip Atlas E2E renders.

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
