# Open Data Coverage & Attributions

GridForge uses strictly commercial-friendly, zero-auth open geospatial data with automatic collar attribution whenever a dataset contributes to a sheet. No API keys, scraped basemaps, or `CC BY-NC` feeds are used.

## Multi-Continent Routing (`AutoDEMSource`)

`AutoDEMSource` routes every sheet to the highest-resolution open elevation provider (`US → Canada → Australia → New Zealand → Global`) with native vertical datums printed on the collar:

- **United States (`usgs3dep`, `NAVD88`, Public Domain):** USGS 3DEP `1 m` LiDAR / `10 m` DEM + `us-trails` (USFS & NPS trails/POIs), `us-public-lands` (USGS PAD-US & USFS Wilderness boundaries), `fema` NFHL flood zones, `nhd` hydrography, `towers` FAA obstacles, and `enc` NOAA charts.
- **Canada (`ca-hrdem`, `CGVD2013`, OGL-Canada):** NRCan CanElevation STAC (`1 m / 2 m HRDEM` → nationwide `mrdem-30`) + `ca-canvec-trails` (Parks Canada trails/POIs, DataBC trails, and ECCC CPCAD protected areas).
- **New Zealand (`nz-linz`, `NZVD2016`, CC BY 4.0):** AWS Open Data `s3://nz-elevation` (`1 m` LiDAR → `8 m` DEM) + `nz-doc-trails` (DOC Great Walks, backcountry huts, campsites & LINZ peaks).
- **Australia (`au-elvis`, `AHD`, CC BY):** Tier-0 Queensland `1 m / 2 m` State LiDAR → GA National `5 m` LiDAR → DEA SRTM 1 s, national overlays (`au-hydro`, `au-parks`, `au-infra`, `au-aviation`), plus all **8 States & Territories** below.
- **Global Fallback (`glo30`, `EGM2008`):** Copernicus WorldDEM-30 (`30 m`) paired with OpenStreetMap.

## Australian State & Territory Trail Overlays

| State / Mode | CLI Flag | Official Open-Data Agency & Datasets | License |
| :--- | :--- | :--- | :--- |
| **Auto (All 8 States)** | `--au-overlay au-state-trails` | Automatically merges intersecting states (e.g. NSW/ACT or NSW/VIC borders), bundles into offline packs, and credits only contributing states | **CC BY** (per-state) |
| **New South Wales** | `--au-overlay au-nsw-trails` | **NSW Spatial Services (DCS)** — fire trails/tracks, POIs, huts & GDA2020 trigs | **CC BY 4.0** |
| **Victoria** | `--au-overlay au-vic-trails` | **Victoria DEECA / DTP Vicmap WFS** — 4WD/fire access, walking tracks, alpine huts & survey marks | **CC BY 4.0** |
| **Tasmania** | `--au-overlay au-tas-trails` | **the LIST (NRE Tas)** — Overland/walking tracks, fire trails, alpine huts & survey trigs | **CC BY 3.0 AU** |
| **Queensland** | `--au-overlay au-qld-trails` | **QSpatial (DESI / DNRM)** — QPWS Great Walks, firelines, parks facilities & survey control | **CC BY 4.0** |
| **ACT** | `--au-overlay au-act-trails` | **ACTmapi / EPSDD** — Namadgi/Tidbinbilla fire trails, tracks, huts & AHD71 survey marks | **CC BY 4.0** |
| **Western Australia** | `--au-overlay au-wa-trails` | **DBCA AGOL & Landgate SLIP** — tracks/trails, fire breaks, fire towers & geodetic marks *(excludes CC BY-NC)* | **CC BY 4.0** |
| **South Australia** | `--au-overlay au-sa-trails` | **DEW / DHUD / Location SA + GA Topo250K** — Heysen/Mawson trails (`/vsizip/` low-RAM), fire tracks & AHD marks | **CC BY 4.0** |
| **Northern Territory** | `--au-overlay au-nt-trails` | **NTLIS / DEPWS WFS + NTG Roads + GA Topo250K** — Larapinta/4WD tracks, lookouts & AHD survey marks | **CC BY 4.0** |

## Data Sources, Attributions & License

| Dataset / Component | Scope & Vertical Datum | License & Attribution |
| :--- | :--- | :--- |
| **GridForge Source Code** | Application, styles, icons, web studio | **GNU GPL v3.0 or later** ([LICENSE](../LICENSE)) |
| **OpenStreetMap** | Global roads, trails, rail, water, buildings, landcover, POIs | **ODbL** — Map data © OpenStreetMap contributors (`openstreetmap.org/copyright`) |
| **USGS 3DEP / NHD / FEMA / FAA / NOAA** | US 1 m & 10 m DEM (`NAVD88`), NHD streams, NFHL flood zones, DOF obstacle towers, ENC charts | **US Public Domain** |
| **USFS / NPS / USGS PAD-US** | US National Forest & National Park Trails/POIs (`us-trails`) and Federal Public Lands & Wilderness (`us-public-lands`) | **US Public Domain** — U.S. Forest Service, National Park Service & USGS PAD-US |
| **NRCan CanElevation (HRDEM / MRDEM)** | Canada 1 m / 2 m LiDAR & 30 m MRDEM (`CGVD2013`, via `ca-hrdem`) | **Open Government Licence – Canada** — Natural Resources Canada (NRCan) |
| **Parks Canada / DataBC / ECCC CPCAD** | Canada National Park Trails/POIs, BC Recreation Trails & Protected Areas (`ca-canvec-trails`) | **Open Government Licence – Canada / BC** — Parks Canada, ECCC CPCAD & DataBC |
| **Geoscience Australia / DEA** | Australia 5 m LiDAR (2025) & SRTM 1s (`AHD`), Surface Hydrology, Electricity/Pipelines, Aviation/Homesteads, Topo250K tracks | **CC BY 4.0** — © Commonwealth of Australia (Geoscience Australia / Digital Earth Australia) |
| **Queensland QldDem LiDAR** | Queensland Tier-0 1 m / 2 m State LiDAR (`AHD`, via `au-elvis`) | **CC BY 4.0** — © State of Queensland (Department of Resources) QldDem LiDAR |
| **DCCEEW CAPAD 2025** | Australia National Parks, Nature Reserves, IPAs | **CC BY 4.0** — © Commonwealth of Australia (DCCEEW) |
| **Australian State & Territory Agencies** | Fire trails, walking tracks, alpine huts, campgrounds & survey trigs across NSW, VIC, TAS, QLD, ACT, WA, SA & NT (`au-state-trails`) | **CC BY 4.0 / CC BY 3.0 AU** — NSW Spatial Services, VIC DEECA/Vicmap, TAS the LIST, QLD QSpatial, ACTmapi, WA DBCA/Landgate, SA DEW/DHUD, NT NTLIS |
| **Toitū Te Whenua LINZ** | New Zealand 1 m LiDAR & 8 m DEM (`NZVD2016`, AWS `s3://nz-elevation`, via `nz-linz`) & `NZ_Maunga` peaks | **CC BY 4.0** — Toitū Te Whenua Land Information New Zealand (LINZ) |
| **NZ Dept. of Conservation (DOC)** | New Zealand Great Walks, Tracks, Backcountry Huts & Campsites (`nz-doc-trails`) | **CC BY 4.0** — © Department of Conservation Te Papa Atawhai & Toitū Te Whenua LINZ |
| **Copernicus WorldDEM-30** | Global 30 m elevation (`EGM2008`) | **Copernicus DEM License** — © DLR e.V. 2010–2014 & © Airbus Defence and Space GmbH 2014–2018, EU & ESA |
| **OpenFreeMap (Studio Vector HD)** | Interactive Studio vector preview basemap (`Vector HD`) | **BSD 3-Clause / CC BY 4.0 / ODbL** — OpenFreeMap © OpenMapTiles, OpenStreetMap contributors |
| **NOAA / NCEI WMM2025** | Global magnetic declination & secular variation (`2025.0–2030.0`) | **US Public Domain** |
