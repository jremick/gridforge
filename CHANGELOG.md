# Changelog

All notable changes to **GridForge** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-11-27

High-resolution cartography, public hosted multi-tenant deployment, and Studio UX upgrade release adding `1:10,000` / `1:12,500` scales, `300` / `450` / `600 DPI` print and digital tile controls, true `1.0 m` LiDAR terrain & `2 m` / `5 ft` contours, richer OSM backcountry micro-features across all four themes, a zero-key **Vector HD** (OpenFreeMap) Studio basemap, a resizable/collapsible sidebar & redesigned header, and opt-in public hosted mode (`GRIDFORGE_AUTH_MODE=hosted`, powering [GridMaps](https://gridmaps.net)).

### Added

- **Higher Print & Digital Export Resolution (`300` / `450` / `600 DPI`, `--export-dpi`):**
  - Exposed inline **Print PDF DPI** and **Digital Export Tiles DPI** selectors in Studio (`web/src/components/sidebar.ts`), wired through URL permalinks, Saved Projects, Copy CLI (`--dpi`, `--export-dpi`), and backend job execution (`src/gridforge/cli.py`, `src/gridforge/web/worker.py`).
  - Added role-aware DPI caps (`GRIDFORGE_GUEST_MAX_DPI=300`, `GRIDFORGE_FRIEND_MAX_DPI=600`) in `src/gridforge/web/limits.py` and `web/src/session.ts`.
- **Finer Tactical Scales (`1:10,000`, `1:12,500`), True `1.0 m` LiDAR Terrain & `2 m` / `5 ft` Contours:**
  - Expanded `VALID_SCALES` to `(10000, 12500, 15000, 24000, 25000, 48000, 50000)` with calibrated metric/imperial collar scale bars (`src/gridforge/render/layout.py`) and 100 mm Calibration Romer scale notes (`src/gridforge/geo/supplements.py`).
  - Preserved true **`1.0 m` LiDAR** terrain on `<= 1:15,000` sheets with a `4096 px` memory safety cap (`src/gridforge/pipeline/terrain.py`), unlocked **`2 m` / `5 ft`** high-density contours on high-resolution DEMs (`<= 10 m`), and upgraded Queensland `QldDem` fetching (`src/gridforge/sources/dem_au_elvis.py`) to `1.0 m` (`qld-1m-lidar`) within verified `4096 × 2048 px` ImageServer limits.
- **Richer OSM Backcountry Cartography Across All 4 Themes:**
  - Expanded `config/osmconf.ini`, `src/gridforge/pipeline/normalize.py`, and `src/gridforge/sources/osm_overpass.py` (`v4:` cache key) to capture cliffs/escarpments, waterfalls/rapids, natural springs, stream fords, cave entrances, survey points/trigs, cairns/rock outcrops, gates/stiles/cattle grids, walls/fences, and historic ruins/forts.
  - Styled all new backcountry features plus distinct `highway=steps` and rough `tracktype=grade4|grade5` 4WD tracks across `color`, `bw`, `night-red` (preserving `R <= 61` red-headlamp legibility), and `alpine`, regenerating `transport.qml` and `points.qml` across all 4 themes.
- **Retina Interactive Studio Basemap (`Vector HD`), Redesigned Header & Resizable Sidebar:**
  - Added a lazy-loaded, zero-API-key **Vector HD** basemap powered by **OpenFreeMap Liberty** (`web/src/map.ts`) preserving all 18 GeoJSON overlays and instant toggling with OpenTopoMap, OSM Standard, Esri Imagery, and Offline MBTiles.
  - Redesigned the Studio header (`web/src/components/telemetry.ts`) with zero mid-character telemetry clipping and an accessible monogram account dropdown menu, and added a persistent, drag-resizable (`300px` min), hide/show sidebar (`[` hotkey, `web/src/sidebarPrefs.ts`).
- **Opt-In Hosted Multi-Tenant Mode (`GRIDFORGE_AUTH_MODE=hosted`), Custom Branding & Single-Container Deployment:**
  - Added SQLite-backed `Guest` / invite-only `Friend` / `Admin` roles (`src/gridforge/web/{auth,limits,cache_prune,entrypoint}.py`), email/password + Google OAuth, Cloudflare Turnstile bot protection, strict per-owner job/project isolation, Admin usage dashboard & public-render kill switch, role-aware artifact TTL + LRU cache pruning, configurable SSR/collar branding (`GRIDFORGE_BRAND_NAME`, `GRIDFORGE_BRAND_SUBTITLE`, `GRIDFORGE_BRAND_TAGLINE`), single-container Railway config (`railway.toml`), and a dedicated deployment guide ([`deploy/hosted-mode.md`](deploy/hosted-mode.md)) while preserving zero-login `127.0.0.1` local defaults (`GRIDFORGE_AUTH_MODE=none`).
- **Automated Verification (`534` offline pytest + `35` frontend tests):**
  - Added hosted E2E suites (`tests/e2e/`) and unit tests (`tests/unit/test_auth_and_limits.py`, `test_container_entrypoint.py`, `test_db_tenancy.py`, `test_worker_pruning.py`, plus `web/tests/*.test.mjs`) and mounted repo configs + `web/src` in `.github/workflows/ci.yml` so all 534 offline backend tests run in CI.

## [1.4.0] - 2026-11-26

Major tactical communications, multi-theme cartography, North America backcountry coverage, offline basemap, and field-device export release shipping four cartographic themes (`172` native QGIS 3.40 `.qml` styles), curved-Earth RF Fresnel Link Cards, tactical range rings & sectors, GeoPackage / OsmAnd / Field Bundle `.zip` exports, Saved Trip Projects, turnkey pure-GDAL offline basemaps across 29 global regions, and full US & Canada backcountry open-data sources.

### Added

- **Phase 0 — Live Multi-Arch GHCR Release Pipeline & Studio Polish:**
  - Added `workflow_dispatch` tag trigger (`tag` input + semver resolution) to `.github/workflows/release.yml` and published live multi-arch (`linux/amd64` + `linux/arm64`) images to `ghcr.io/jremick/gridforge`.
  - Auto-clears Copy CLI / form error banners in Studio on control edits and organizes Quick-Jump Presets into 4 regional tabs (`NSW/VIC`, `AU`, `NZ`, `NA` — 32 curated backcountry destinations).
- **Track 1 — Point-to-Point Curved-Earth RF / 1st Fresnel Link Profile & Link Card PDF (`--rf-link`, `--rf-link-card`, `POST /api/rf-link-profile`):**
  - Curved-Earth (`k=4/3`) terrain & 1st Fresnel zone clearance engine (`src/gridforge/pipeline/terrain.py`) computing `CLEAR` / `MARGINAL` / `OBSTRUCTED` status, 60% F1 clearance, exact FSPL (dB), True/Grid/WMM2025 Magnetic bearings, NATO mils, and 10 m MGRS endpoints across 200 bilinear DEM samples.
  - Pure-vector **RF / LOS Fresnel Link Card** companion PDF page (`render_rf_link_card_pdf` in `src/gridforge/geo/supplements.py`) and interactive MapLibre Studio `⚡ A ↔ B` picker with band presets (`LoRa 915`, `UHF CB 477`, `GMRS 462`, `VHF 146`, `LoRa 868`, `2.4 GHz`, `5.8 GHz`) and live inline SVG Fresnel profile chart.
- **Track 2 — Turnkey Pure-GDAL Offline Basemap Builder & 29-Region Global Pack Manager (`gridforge basemap-pack`, `include_basemap`):**
  - Pure GDAL/NumPy/PIL hypsometric color-relief + multi-directional hillshade Web Mercator `.mbtiles` builder (`src/gridforge/packs/basemap_builder.py`, zoom `7–12`, atomic via `staged_archive`, `< 300 MB` RAM, zero QGIS/Qt imports).
  - Expanded offline pack catalog (`src/gridforge/packs/regions.py`) to **29 grouped regions** (`au_tactical`: 7, `au_state`: 8, `nz`: 6, `na`: 8 — 5 US + 3 Canada) with `include_basemap=True` bundling `<pack_dir>/basemap.mbtiles` and per-pack discovery (`GET /api/basemap/info?pack_id=` & `GET /api/basemap/{z}/{x}/{y}.{ext}?pack_id=`).
- **Track 3 — `night-red` & `alpine` Themes (`172` Native QGIS `.qml`s) + Tactical Ruler, Range Rings & Sectors (`--mode night-red|alpine`, `--range-ring`, `--tactical-line`, `POST /api/ruler-metrics`):**
  - **`night-red`:** Red-headlamp-safe cartographic theme with strictly low-Red (`R <= 61`) ink on all critical terrain, road, trail, water, grid, viewshed, and tactical layers so nothing disappears under red night-vision illumination.
  - **`alpine`:** Winter/ski-mountaineering theme with snow canvas, glacier/snowfield polygons, Swiss glacier-blue contours, and boosted `0.68` avalanche slope shading.
  - Regenerated all **43 layer roles × 4 themes = 172 native QGIS 3.40 `.qml` files**.
  - Added interactive Studio `📏 Ruler` (live geodesic distance, True/Grid/Mag bearing + NATO mils + DEM `%` grade + `Pin to PDF`), `◎ Ring` concentric geodesic range rings, and `⬠ Sector` polygon AOI markup (`src/gridforge/geo/tactical.py`) rendered crisply onto print sheets.
- **Track 4 — OGC GeoPackage Tiles, OsmAnd `.sqlitedb`, Field Bundle `.zip` & Saved Trip Projects (`--gpkg`, `--osmand`, `--field-bundle`, `/api/projects`):**
  - Atomic OGC GeoPackage raster tiles (`.gpkg`, `GoogleMapsCompatible` EPSG:3857 + overviews), OsmAnd `.sqlitedb` (`BigPlanet` `z=17-zoom`), and `<slug>_field_bundle.zip` bundling all enabled artifacts alongside valid GPX 1.1 (`waypoints`, `viewsheds`, `rf_links`, `tactical_graphics`) and `README_FIELD_BUNDLE.txt`.
  - Added SQLite WAL **Saved Trip Projects** CRUD (`GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}` under `SameOriginWritesMiddleware`), `GET /api/jobs/{id}/thumbnail.png`, and Studio **Projects** slide-over drawer (Save/Load/Clone/Delete with map sheet thumbnails).
- **Track 5 — Strong North America Coverage (US Trails/Public Lands & Canada NRCan HRDEM/Parks Canada):**
  - **United States (`--us-overlay us-trails`, `--us-overlay us-public-lands`):** Live Public Domain **USFS** (`EDW_TrailNFSPublish_01`, `EDW_RecreationOpportunities_01`, `EDW_Wilderness_01`), **NPS** (`NPS_Public_Trails`, `NPS_Public_POIs_Geographic`), and **USGS PAD-US** federal land managers & wilderness boundaries.
  - **Canada (`--dem-source ca-hrdem`, `--ca-overlay ca-canvec-trails`):** Live OGL-Canada **NRCan CanElevation STAC** (`hrdem-mosaic-1m` -> `hrdem-mosaic-2m` -> nationwide `mrdem-30` in `CGVD2013` -> Copernicus GLO-30) plus **Parks Canada** trails/POIs, **DataBC** active forestry recreation trails (BC), and **ECCC CPCAD** protected areas.
  - Upgraded `AutoDEMSource` to accurate **5-way** routing (`US -> CA -> AU -> NZ -> Global`) with piecewise US–Canada international border classification.
- **Automated Verification (`293` offline pytest + `6` Copy CLI + `3/3` live US/Canada/AU E2E):**
  - Added `tests/unit/test_v14_rf_and_na.py`, `tests/unit/test_v14_exports_and_packs.py`, `tests/unit/test_v14_web_and_rf.py`, and `tests/integration/test_v14_features.py` verifying Yosemite CA (`alpine`), Banff AB (`night-red`, `CGVD2013`), and Mt Kosciuszko (RF Fresnel Link Card, Tactical Rings/Sector, `.gpkg`/`.sqlitedb`/`.zip`).

## [1.3.0] - 2026-11-26

Major operational, geospatial, self-hosting, and hardening release adding RF/LOS Viewshed mapping, New Zealand 1 m LiDAR & DOC trails, Queensland Tier-0 2 m State LiDAR, Atlas Overview Index Covers & Fold/Join Marks, Studio Offline Pack Manager & Air-Gapped Basemap Mode, multi-arch GHCR CI/CD + homelab reverse-proxy guides, and five production security/reliability hardening improvements (Codex PRs `#1`–`#5`).

### Added

- **RF / Line-of-Sight Viewshed Mapping (`--viewshed`, `POST /api/viewshed-preview`, Studio `📡 LOS` / `V` hotkey):**
  - Multi-observer `gdal_viewshed` terrain visibility engine (`src/gridforge/pipeline/terrain.py`) rendering translucent emerald LOS coverage on the bottom raster stack (keeping all contours, roads, trails, and MGRS lines pure vector) paired with dashed range rings and `observer.svg` antenna beacons with AGL height callouts (`22` SVG icons, `78` native QGIS 3.40 `.qml` styles).
  - Interactive MapLibre Studio `📡 LOS` tool drops observers directly on the map, previews terrain-masked coverage (`Visible: XX.X km²`), displays 10 m MGRS and ground elevation, and tunes observer height, target height, and max range (`250 m`–`25 km`) before printing.
- **New Zealand Support — LINZ 1 m LiDAR / 8 m DEM (`NZVD2016`) & DOC Trails/Huts/Peaks (`--dem-source nz-linz`, `--nz-overlay nz-doc-trails`):**
  - Streams zero-auth LERC COGs directly from AWS Open Data `s3://nz-elevation` via closed-form NZTM2000 Topo50 / 1:10k indexing (`1 m LiDAR -> 8 m DEM -> Copernicus GLO-30` fallback) and integrates into 4-way `AutoDEMSource` (`US -> AU -> NZ -> Global`).
  - Live CC BY 4.0 **Department of Conservation (DOC)** Great Walks, walking tracks, alpine huts, campgrounds, and **Toitū Te Whenua LINZ** `NZ_Maunga` peaks (plus optional `LINZ_API_KEY` geodetic survey marks) rendered via the unified backcountry trail/POI schema.
- **Australia Tier-0 Queensland 2 m State LiDAR (`dem_au_elvis.py`):**
  - Automatically upgrades Queensland sheets inside completed `QldDem` LiDAR projects to **2 m State LiDAR (`AHD`, CC BY 4.0)** with shoreline cliff-ratio guards before seamlessly falling through to GA National 5 m LiDAR WCS and DEA SRTM 1 s.
- **Atlas Overview & Sheet Index Cover Page + Edge Fold/Join Marks (`--atlas-index`, `--fold-marks`):**
  - Prepends a mathematically exact pure-vector **Atlas Overview & Sheet Index Cover** (`src/gridforge/geo/supplements.py`) showing scaled sheet boxes (`A1..C3` or `S1..Sn`), diagonal overlap hatching, GPX/GeoJSON route polyline, North arrow, scale bar, and a complete 10 m MGRS & UTM extent table.
  - Adds optional `4.0 mm` L-shaped corner registration crop marks and midpoint `FOLD / JOIN` edge ticks outside the neatline strictly clear of collar metadata and MGRS labels.
- **Studio Offline Pack Manager & Air-Gapped Web Basemap Mode (`src/gridforge/web/{basemap,pack_tasks}.py`, `packsModal.ts`):**
  - One-click **Manage Packs** modal in Print Studio backed by `GET /api/packs/regions` (all 15 Australian presets + custom BBox), single-concurrency `POST /api/packs/build` with live stage polling (`GET /api/packs/tasks`), and path-traversal-hardened `DELETE /api/packs/{pack_id}`.
  - Zero-external-request **Air-Gapped Web Basemap Mode**: mount any raster `.mbtiles` archive (`GRIDFORGE_BASEMAP_MBTILES` or `/data/basemap.mbtiles`) to serve XYZ->TMS flipped tiles via `GET /api/basemap/info` & `GET /api/basemap/{z}/{x}/{y}.{ext}` with an automatic **Offline** switcher pill in MapLibre Studio.
- **OSS CI/CD, Prebuilt Multi-Arch GHCR Images & Homelab/Contributor Docs:**
  - Added `.github/workflows/ci.yml` (Node 22 Copy CLI tests + SPA build + Buildx Docker offline pytest suite) and `.github/workflows/release.yml` (multi-arch `linux/amd64,linux/arm64` push to `ghcr.io/jremick/gridforge:{latest,vX.Y,vX.Y.Z}` on tag push).
  - Added zero-build `docker-compose.ghcr.yml`, homelab HTTPS guides (`deploy/Caddyfile.example`, `deploy/traefik-compose.override.yml`, `deploy/tailscale-serve.md`), `CONTRIBUTING.md`, `docs/STYLING_GUIDE.md`, and GitHub issue/PR templates.

### Fixed & Hardened (Codex PRs `#1`–`#5`)

- **Local-First Studio Security Defaults (PR `#1`):** Bound Compose ports and `gridforge serve` / Vite dev servers strictly to `127.0.0.1` by default, removed wildcard CORS, added `SameOriginWritesMiddleware` (`src/gridforge/web/access.py`) protecting all non-GET/HEAD/OPTIONS routes (`403` on cross-origin writes) plus `TrustedHostMiddleware` (`GRIDFORGE_ALLOWED_HOSTS`, `FORWARDED_ALLOW_IPS`), and pinned `starlette>=1.9` for IPv6 `[::1]` and mixed-case `Host` support.
- **Strict Air-Gapped Partial Pack Handling (PR `#2`):** Offline renders/exports using `--pack` / `--pack-id` now fail cleanly before any online fetch when a partial pack omits DEM or OSM PBF (while allowing completion via `--offline-dem` / `--offline-pbf`), and Studio preserves unavailable or `?pack=` permalink selections without silent fallback.
- **Atomic Offline Device Archive Publication (PR `#3`):** Added `src/gridforge/export/_atomic.py` (`staged_archive`) so Garmin `.kmz` and ATAK `.mbtiles` exports stage atomically on the destination filesystem, properly close SQLite handles, protect `-journal`/`-wal`/`-shm`/`.aux.xml` sidecars, and clean scratch files prior to final atomic rename.
- **Multi-Sheet Atlas Projection & Antimeridian Validation (PR `#4`):** Added shared `compute_atlas_layouts` and antimeridian (`±180°`) extent rejection in `src/gridforge/geo/crs.py`, enforced consistently across `/api/footprint`, `/api/jobs`, and `worker.py`, with digital-only `1×1` footprint preview and stale-footprint clearing on validation errors in Studio.
- **Reproducible Copy CLI & Manifest-ID Pack Lookup (PR `#5`):** Added `resolve_pack_directory` (`--pack-id`) supporting renamed pack folders, restored single-sheet `--elevation-profile` from uploaded `--overlay` routes, switched Studio **Copy CLI** to POSIX single-quoted `docker compose run --rm cli gridforge ...` targeting `/out/` and `/out/inputs/` with unsupported-combo clipboard guards, and added automated `web/tests/copy-cli.test.mjs` (`5/5` passing) + `264` offline pytest checks.

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
