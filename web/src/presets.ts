import type {
  AUOverlayKind,
  AUStateTrailKind,
  AppFormState,
  AtlasGridOption,
  BasemapId,
  CAOverlayKind,
  ColorMode,
  DemSourceKind,
  DensityLevel,
  HillshadeStrength,
  LocationPreset,
  NZOverlayKind,
  PaperSize,
  PresetRegionTab,
  USOverlayKind,
  WaypointCategory,
} from "./types";

export const DEFAULT_CENTER_LAT = -33.732;
export const DEFAULT_CENTER_LON = 150.312;
export const DEFAULT_MAP_ZOOM = 12;

export const PRESET_REGION_TABS: readonly {
  id: PresetRegionTab;
  label: string;
}[] = [
  { id: "nsw-vic", label: "NSW/VIC" },
  { id: "au", label: "AU" },
  { id: "nz", label: "NZ" },
  { id: "na", label: "NA" },
];

export const LOCATION_PRESETS: readonly LocationPreset[] = [
  // Default NSW / VIC tab (preserved exact 8 cards!)
  {
    id: "blue-mountains",
    tab: "nsw-vic",
    label: "Blue Mountains NSW",
    regionTag: "NSW · 56H",
    lat: -33.732,
    lon: 150.312,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "BLUE MOUNTAINS — KATOOMBA",
    zoom: 12,
  },
  {
    id: "mt-kosciuszko",
    tab: "nsw-vic",
    label: "Mt Kosciuszko NSW",
    regionTag: "NSW · 55H",
    lat: -36.456,
    lon: 148.263,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "MT KOSCIUSZKO — MAIN RANGE",
    zoom: 12,
  },
  {
    id: "budawangs",
    tab: "nsw-vic",
    label: "Budawangs NSW",
    regionTag: "NSW · 56H",
    lat: -35.276,
    lon: 150.193,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "BUDAWANGS — THE CASTLE",
    zoom: 12,
  },
  {
    id: "warrumbungles",
    tab: "nsw-vic",
    label: "Warrumbungles NSW",
    regionTag: "NSW · 55J",
    lat: -31.298,
    lon: 149.005,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "WARRUMBUNGLES — GRAND HIGH TOPS",
    zoom: 12,
  },
  {
    id: "grampians",
    tab: "nsw-vic",
    label: "Grampians VIC",
    regionTag: "VIC · 54H",
    lat: -37.145,
    lon: 142.515,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "GRAMPIANS — HALLS GAP",
    zoom: 12,
  },
  {
    id: "wilsons-prom",
    tab: "nsw-vic",
    label: "Wilsons Prom VIC",
    regionTag: "VIC · 55H",
    lat: -39.032,
    lon: 146.365,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "WILSONS PROMONTORY — TIDAL RIVER",
    zoom: 12,
  },
  {
    id: "mt-bogong",
    tab: "nsw-vic",
    label: "Mt Bogong VIC",
    regionTag: "VIC · 55H",
    lat: -36.733,
    lon: 147.306,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "MT BOGONG — ALPINE NP",
    zoom: 12,
  },
  {
    id: "cathedral-range",
    tab: "nsw-vic",
    label: "Cathedral Range VIC",
    regionTag: "VIC · 55H",
    lat: -37.392,
    lon: 145.752,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "CATHEDRAL RANGE — SUGARLOAF",
    zoom: 12,
  },

  // AU (Pan-Australia)
  {
    id: "cradle-mountain",
    tab: "au",
    label: "Cradle Mountain TAS",
    regionTag: "TAS · 55G",
    lat: -41.684,
    lon: 145.951,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-state-trails", "au-hydro", "au-parks"],
    defaultTitle: "CRADLE MOUNTAIN — OVERLAND",
    zoom: 12,
  },
  {
    id: "lamington-qld",
    tab: "au",
    label: "Lamington NP QLD",
    regionTag: "QLD · 56J",
    lat: -28.2,
    lon: 153.19,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-state-trails", "au-hydro", "au-parks"],
    defaultTitle: "LAMINGTON — BINNA BURRA",
    zoom: 12,
  },
  {
    id: "namadgi-act",
    tab: "au",
    label: "Namadgi NP ACT",
    regionTag: "ACT · 55H",
    lat: -35.64,
    lon: 148.98,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-state-trails", "au-hydro", "au-parks"],
    defaultTitle: "NAMADGI — ORRORAL VALLEY",
    zoom: 12,
  },
  {
    id: "stirling-range-wa",
    tab: "au",
    label: "Stirling Range WA",
    regionTag: "WA · 50H",
    lat: -34.375,
    lon: 118.255,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-state-trails", "au-hydro", "au-parks"],
    defaultTitle: "STIRLING RANGE — BLUFF KNOLL",
    zoom: 12,
  },
  {
    id: "flinders-ranges-sa",
    tab: "au",
    label: "Flinders Ranges SA",
    regionTag: "SA · 54J",
    lat: -31.53,
    lon: 138.6,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-state-trails", "au-hydro", "au-parks"],
    defaultTitle: "FLINDERS RANGES — WILPENA",
    zoom: 12,
  },
  {
    id: "west-macdonnell-nt",
    tab: "au",
    label: "Larapinta Trail NT",
    regionTag: "NT · 53K",
    lat: -23.73,
    lon: 133.24,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-state-trails", "au-hydro", "au-parks"],
    defaultTitle: "WEST MACDONNELL — LARAPINTA",
    zoom: 12,
  },
  {
    id: "uluru-nt",
    tab: "au",
    label: "Uluru / Kata Tjuta NT",
    regionTag: "NT · 52J",
    lat: -25.3444,
    lon: 131.0369,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks", "au-aviation"],
    defaultTitle: "ULURU — KATA TJUTA NT",
    zoom: 12,
  },
  {
    id: "sydney-harbour",
    tab: "au",
    label: "Sydney Harbour NSW",
    regionTag: "NSW · 56H",
    lat: -33.8568,
    lon: 151.2153,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "auto",
    au_overlays: ["au-hydro", "au-parks"],
    defaultTitle: "SYDNEY HARBOUR — NORTH SHORE",
    zoom: 12.5,
  },

  // NZ (New Zealand)
  {
    id: "tongariro-nz",
    tab: "nz",
    label: "Tongariro Crossing",
    regionTag: "NZ · 60H",
    lat: -39.133,
    lon: 175.642,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "TONGARIRO ALPINE CROSSING",
    zoom: 12,
  },
  {
    id: "aoraki-mt-cook-nz",
    tab: "nz",
    label: "Aoraki / Mt Cook",
    regionTag: "NZ · 59G",
    lat: -43.735,
    lon: 170.096,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "AORAKI — MOUNT COOK NP",
    zoom: 12,
  },
  {
    id: "milford-track-nz",
    tab: "nz",
    label: "Milford Track",
    regionTag: "NZ · 58G",
    lat: -44.788,
    lon: 167.775,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "FIORDLAND — MILFORD TRACK",
    zoom: 12,
  },
  {
    id: "routeburn-nz",
    tab: "nz",
    label: "Routeburn Track",
    regionTag: "NZ · 58G",
    lat: -44.73,
    lon: 168.176,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "ROUTEBURN — HARRIS SADDLE",
    zoom: 12,
  },
  {
    id: "arthurs-pass-nz",
    tab: "nz",
    label: "Arthur's Pass",
    regionTag: "NZ · 59G",
    lat: -42.942,
    lon: 171.561,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "ARTHUR'S PASS — AVALANCHE PK",
    zoom: 12,
  },
  {
    id: "abel-tasman-nz",
    tab: "nz",
    label: "Abel Tasman Coast",
    regionTag: "NZ · 59G",
    lat: -40.916,
    lon: 173.046,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "ABEL TASMAN COAST TRACK",
    zoom: 12,
  },
  {
    id: "mt-taranaki-nz",
    tab: "nz",
    label: "Mt Taranaki",
    regionTag: "NZ · 60H",
    lat: -39.297,
    lon: 174.063,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "TE PAPAKURA O TARANAKI",
    zoom: 12,
  },
  {
    id: "wanaka-roys-nz",
    tab: "nz",
    label: "Mt Aspiring / Wanaka",
    regionTag: "NZ · 59G",
    lat: -44.692,
    lon: 169.046,
    scale: 25000,
    paper: "a4",
    units: "metric",
    dem_source: "nz-linz",
    nz_overlays: ["nz-doc-trails"],
    defaultTitle: "WANAKA — ROYS PEAK",
    zoom: 12,
  },

  // NA (North America — US + Canada)
  {
    id: "boulder-co",
    tab: "na",
    label: "Boulder Flatirons CO",
    regionTag: "US · 13T",
    lat: 40.005,
    lon: -105.285,
    scale: 24000,
    paper: "letter",
    units: "imperial",
    dem_source: "auto",
    us_overlays: ["us-trails", "us-public-lands"],
    defaultTitle: "BOULDER — FLATIRONS CO",
    zoom: 12,
  },
  {
    id: "yosemite-ca",
    tab: "na",
    label: "Yosemite Valley CA",
    regionTag: "US · 11S",
    lat: 37.745,
    lon: -119.533,
    scale: 24000,
    paper: "letter",
    units: "imperial",
    dem_source: "auto",
    us_overlays: ["us-trails", "us-public-lands"],
    defaultTitle: "YOSEMITE — HALF DOME CA",
    zoom: 12,
  },
  {
    id: "mt-rainier-wa",
    tab: "na",
    label: "Mt Rainier WA",
    regionTag: "US · 10T",
    lat: 46.786,
    lon: -121.735,
    scale: 24000,
    paper: "letter",
    units: "imperial",
    dem_source: "auto",
    us_overlays: ["us-trails", "us-public-lands"],
    defaultTitle: "MT RAINIER — PARADISE WA",
    zoom: 12,
  },
  {
    id: "grand-teton-wy",
    tab: "na",
    label: "Grand Teton WY",
    regionTag: "US · 12T",
    lat: 43.741,
    lon: -110.752,
    scale: 24000,
    paper: "letter",
    units: "imperial",
    dem_source: "auto",
    us_overlays: ["us-trails", "us-public-lands"],
    defaultTitle: "GRAND TETON — JENNY LAKE",
    zoom: 12,
  },
  {
    id: "banff-ab",
    tab: "na",
    label: "Banff / Lake Louise AB",
    regionTag: "CA · 11U",
    lat: 51.416,
    lon: -116.216,
    scale: 25000,
    paper: "letter",
    units: "metric",
    dem_source: "ca-hrdem",
    ca_overlays: ["ca-canvec-trails"],
    defaultTitle: "BANFF — LAKE LOUISE AB",
    zoom: 12,
  },
  {
    id: "squamish-bc",
    tab: "na",
    label: "Squamish / Garibaldi BC",
    regionTag: "CA · 10U",
    lat: 49.946,
    lon: -123.025,
    scale: 25000,
    paper: "letter",
    units: "metric",
    dem_source: "ca-hrdem",
    ca_overlays: ["ca-canvec-trails"],
    defaultTitle: "GARIBALDI — SQUAMISH BC",
    zoom: 12,
  },
  {
    id: "white-mtns-nh",
    tab: "na",
    label: "Mt Washington NH",
    regionTag: "US · 19T",
    lat: 44.27,
    lon: -71.303,
    scale: 24000,
    paper: "letter",
    units: "imperial",
    dem_source: "auto",
    us_overlays: ["us-trails", "us-public-lands"],
    defaultTitle: "PRESIDENTIAL RANGE NH",
    zoom: 12,
  },
  {
    id: "zion-ut",
    tab: "na",
    label: "Zion Canyon UT",
    regionTag: "US · 12S",
    lat: 37.269,
    lon: -112.948,
    scale: 24000,
    paper: "letter",
    units: "imperial",
    dem_source: "auto",
    us_overlays: ["us-trails", "us-public-lands"],
    defaultTitle: "ZION — ANGELS LANDING UT",
    zoom: 12,
  },
];

export const SCALE_OPTIONS: readonly { value: number; label: string }[] = [
  { value: 10000, label: "1:10,000" },
  { value: 12500, label: "1:12,500" },
  { value: 15000, label: "1:15,000" },
  { value: 24000, label: "1:24,000" },
  { value: 25000, label: "1:25,000" },
  { value: 48000, label: "1:48,000" },
  { value: 50000, label: "1:50,000" },
];

export const DPI_OPTIONS: readonly {
  value: number;
  label: string;
  shortLabel: string;
}[] = [
  { value: 300, label: "300 DPI (Standard)", shortLabel: "300 DPI" },
  { value: 450, label: "450 DPI (High)", shortLabel: "450 DPI" },
  { value: 600, label: "600 DPI (Ultra)", shortLabel: "600 DPI" },
];

export const PAPER_OPTIONS: readonly {
  value: PaperSize;
  label: string;
  dims: string;
}[] = [
  { value: "a4", label: "A4", dims: "210 × 297 mm" },
  { value: "letter", label: "Letter", dims: "8.5 × 11 in" },
  { value: "legal", label: "Legal", dims: "8.5 × 14 in" },
  { value: "a3", label: "A3", dims: "297 × 420 mm" },
];

export const COLOR_MODE_OPTIONS: readonly {
  value: ColorMode;
  label: string;
  swatch: string;
  palette: readonly [string, string, string];
  hint: string;
}[] = [
  {
    value: "color",
    label: "Color",
    swatch: "#22C55E",
    palette: ["#F4F0E2", "#4D9A57", "#B45309"],
    hint: "Standard topographic color print palette",
  },
  {
    value: "bw",
    label: "B&W Print",
    swatch: "#CBD5E1",
    palette: ["#F8FAFC", "#94A3B8", "#1E293B"],
    hint: "High-contrast laser monochrome",
  },
  {
    value: "night-red",
    label: "Night (Red-Light)",
    swatch: "#38BDF8",
    palette: ["#0F172A", "#0EA5E9", "#2DD4BF"],
    hint: "Red-headlamp safe (zero red or orange critical ink)",
  },
  {
    value: "alpine",
    label: "Alpine",
    swatch: "#60A5FA",
    palette: ["#F8FAFC", "#38BDF8", "#9333EA"],
    hint: "Winter/glacier contours and avalanche slope bands",
  },
];

export const ATLAS_GRID_OPTIONS: readonly AtlasGridOption[] = [
  { id: "1x1", label: "1×1", cols: 1, rows: 1 },
  { id: "2x1", label: "2×1", cols: 2, rows: 1 },
  { id: "1x2", label: "1×2", cols: 1, rows: 2 },
  { id: "2x2", label: "2×2", cols: 2, rows: 2 },
  { id: "3x2", label: "3×2", cols: 3, rows: 2 },
  { id: "3x3", label: "3×3", cols: 3, rows: 3 },
];

export const DEM_SOURCE_OPTIONS: readonly {
  value: DemSourceKind;
  label: string;
}[] = [
  { value: "auto", label: "Auto (Best Regional Elevation)" },
  { value: "au-elvis", label: "Australia GA 5m / SRTM (AHD)" },
  { value: "nz-linz", label: "New Zealand LINZ 1 m LiDAR / 8 m (NZVD2016)" },
  { value: "ca-hrdem", label: "Canada NRCan HRDEM 1m / CDEM (CGVD2013)" },
  { value: "usgs3dep", label: "USGS 3DEP 1m / 10m (NAVD88)" },
  { value: "glo30", label: "Copernicus GLO-30 (EGM2008)" },
];

export const AU_STATE_TRAIL_CHIPS: readonly {
  value: AUStateTrailKind;
  label: string;
  name: string;
}[] = [
  { value: "au-nsw-trails", label: "NSW", name: "New South Wales" },
  { value: "au-vic-trails", label: "VIC", name: "Victoria" },
  { value: "au-tas-trails", label: "TAS", name: "Tasmania" },
  { value: "au-qld-trails", label: "QLD", name: "Queensland" },
  { value: "au-wa-trails", label: "WA", name: "Western Australia" },
  { value: "au-sa-trails", label: "SA", name: "South Australia" },
  { value: "au-nt-trails", label: "NT", name: "Northern Territory" },
  { value: "au-act-trails", label: "ACT", name: "Australian Capital Territory" },
];

export const AU_OVERLAY_OPTIONS: readonly {
  value: AUOverlayKind;
  label: string;
  hint: string;
}[] = [
  {
    value: "au-hydro",
    label: "GA Hydrology (Bores/Streams)",
    hint: "Watercourses, bores & waterholes",
  },
  {
    value: "au-parks",
    label: "CAPAD National Parks & IPAs",
    hint: "Protected area boundaries",
  },
  {
    value: "au-infra",
    label: "GA Power & Pipelines",
    hint: "HV lines, substations & pipelines",
  },
  {
    value: "au-aviation",
    label: "GA Airfields & Homesteads",
    hint: "Regional strips, runways & stations",
  },
];

export const ALL_AU_OVERLAY_KINDS: readonly AUOverlayKind[] = [
  "au-state-trails",
  ...AU_STATE_TRAIL_CHIPS.map((c) => c.value),
  ...AU_OVERLAY_OPTIONS.map((o) => o.value),
];

export const NZ_OVERLAY_OPTIONS: readonly {
  value: NZOverlayKind;
  label: string;
  hint: string;
}[] = [
  {
    value: "nz-doc-trails",
    label: "DOC Tracks, Huts & LINZ Trigs",
    hint: "Great Walks, backcountry huts, camps & NZ trigs",
  },
];

export const CA_OVERLAY_OPTIONS: readonly {
  value: CAOverlayKind;
  label: string;
  hint: string;
}[] = [
  {
    value: "ca-canvec-trails",
    label: "Canada CanVec / Parks Trails & Huts",
    hint: "NRCan CanVec / Parks Canada trails, shelters & parks (OGL-CA)",
  },
];

export const US_OVERLAY_OPTIONS: readonly {
  value: USOverlayKind;
  label: string;
  hint: string;
}[] = [
  {
    value: "us-trails",
    label: "USFS / NPS / USGS Trails & POIs",
    hint: "Backcountry trails, trailheads, campgrounds & peaks",
  },
  {
    value: "us-public-lands",
    label: "US Public Lands & Wilderness (PAD-US)",
    hint: "USFS, NPS, BLM & wilderness boundaries",
  },
  {
    value: "fema",
    label: "FEMA Flood Zones",
    hint: "NFHL 100-yr / 500-yr floodplains",
  },
  {
    value: "nhd",
    label: "USGS NHD Streams",
    hint: "High-res national hydrography",
  },
  {
    value: "towers",
    label: "FAA Obstacle Towers",
    hint: "DOF towers, masts & AGL heights",
  },
  {
    value: "enc",
    label: "NOAA ENC Charts",
    hint: "Coastal depth soundings & buoys",
  },
];

export const RF_BAND_PRESETS: readonly { label: string; freq_mhz: number }[] = [
  { label: "LoRa / Meshtastic 915 MHz (AU/US)", freq_mhz: 915 },
  { label: "UHF CB 477 MHz (AU)", freq_mhz: 477 },
  { label: "GMRS / FRS 462 MHz (US)", freq_mhz: 462 },
  { label: "VHF 2m Amateur 146 MHz", freq_mhz: 146 },
  { label: "LoRa 868 MHz (EU/NZ)", freq_mhz: 868 },
  { label: "Wi-Fi / AREDN 2.4 GHz", freq_mhz: 2412 },
  { label: "PtP Microwave 5.8 GHz", freq_mhz: 5800 },
];

export const WAYPOINT_CATEGORY_OPTIONS: readonly {
  value: WaypointCategory;
  label: string;
  color: string;
}[] = [
  { value: "waypoint", label: "WP", color: "#EC4899" },
  { value: "camp", label: "Camp", color: "#10B981" },
  { value: "water", label: "Water", color: "#38BDF8" },
  { value: "hazard", label: "Hazard", color: "#EF4444" },
  { value: "lz", label: "LZ", color: "#F59E0B" },
];

export const DENSITY_OPTIONS: readonly { value: DensityLevel; label: string }[] =
  [
    { value: "low", label: "Low" },
    { value: "normal", label: "Normal" },
    { value: "high", label: "High" },
  ];

export const HILLSHADE_OPTIONS: readonly {
  value: HillshadeStrength;
  label: string;
}[] = [
  { value: "off", label: "Off" },
  { value: "light", label: "Light" },
  { value: "normal", label: "Normal" },
  { value: "strong", label: "Strong" },
];

export const BASEMAP_OPTIONS: readonly { id: BasemapId; label: string }[] = [
  { id: "vector", label: "Vector HD" },
  { id: "opentopo", label: "OpenTopoMap" },
  { id: "osm", label: "OSM Standard" },
  { id: "satellite", label: "Esri Imagery" },
];

export const CARTO_TOGGLES: readonly {
  key:
    | "show_buildings"
    | "show_trees"
    | "show_power"
    | "show_pipelines"
    | "show_grid"
    | "show_river_arrows";
  label: string;
}[] = [
  { key: "show_buildings", label: "Buildings" },
  { key: "show_trees", label: "Trees / Woodland" },
  { key: "show_power", label: "Power Lines" },
  { key: "show_pipelines", label: "Pipelines" },
  { key: "show_grid", label: "MGRS Grid" },
  { key: "show_river_arrows", label: "River Flow Arrows" },
];

export function createInitialFormState(): AppFormState {
  return {
    center: `${DEFAULT_CENTER_LAT}, ${DEFAULT_CENTER_LON}`,
    lat: DEFAULT_CENTER_LAT,
    lon: DEFAULT_CENTER_LON,
    title: "",
    scale: 25000,
    paper: "a4",
    orientation: "portrait",
    color_mode: "color",
    units: "metric",
    dpi: 300,
    grid_cols: 1,
    grid_rows: 1,
    dem_source: "auto",
    pack_id: "",
    au_overlays: [],
    nz_overlays: [],
    ca_overlays: [],
    us_overlays: [],
    carto: {
      contour_density: "normal",
      hillshade_strength: "normal",
      label_density: "normal",
      show_buildings: true,
      show_trees: true,
      show_power: true,
      show_pipelines: true,
      show_grid: true,
      show_river_arrows: true,
      show_slope_shading: false,
    },
    outputs: {
      pdf: true,
      geopdf: false,
      mbtiles: false,
      mbtiles_format: "png",
      kmz: false,
      garmin_profile: "standard",
      include_grid_on_digital: true,
      gpkg_tiles: false,
      osmand_sqlitedb: false,
      field_bundle_zip: false,
      export_dpi: 300,
    },
    overlayFile: null,
    waypoints: [],
    viewsheds: [],
    viewshedDefaults: {
      observer_height_m: 2,
      target_height_m: 1.5,
      max_distance_km: 10,
    },
    rfLinks: [],
    rfLinkDefaults: {
      freq_mhz: 915,
      from_height_m: 2,
      to_height_m: 2,
    },
    tacticalGraphics: [],
    stripPlan: null,
    include_calibration_page: false,
    include_route_card: false,
    include_elevation_profile: false,
    include_atlas_index: false,
    include_rf_link_card: false,
    show_fold_marks: false,
  };
}

export function isStripEligibleFile(file: File | null): boolean {
  if (!file) return false;
  const lower = file.name.toLowerCase();
  return (
    lower.endsWith(".gpx") ||
    lower.endsWith(".geojson") ||
    lower.endsWith(".json")
  );
}

const LAT_LON_RE =
  /^\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*$/;
const LAT_LON_SPACE_RE =
  /^\s*([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)\s*$/;
const MGRS_RE =
  /^\s*0?(?:[1-9]|[1-5]\d|60)\s*[C-HJ-NP-X]\s*[A-HJ-NP-Z]{2}(?:\s*\d{2,10})?\s*$/i;

export function parseLatLon(
  value: string
): { lat: number; lon: number } | null {
  const m = LAT_LON_RE.exec(value) ?? LAT_LON_SPACE_RE.exec(value);
  if (!m) return null;
  const lat = Number(m[1]);
  const lon = Number(m[2]);
  if (
    !Number.isFinite(lat) ||
    !Number.isFinite(lon) ||
    lat < -80 ||
    lat > 84 ||
    lon < -180 ||
    lon > 180
  ) {
    return null;
  }
  return { lat, lon };
}

export function looksLikeCoordOrMgrs(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (parseLatLon(trimmed) !== null) return true;
  return MGRS_RE.test(trimmed);
}

export function formatCoords(lat: number, lon: number): string {
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

export function formatSignedDeg(deg: number): string {
  const abs = Math.abs(deg).toFixed(1);
  if (Math.abs(deg) < 0.05) return "0.0°";
  return `${deg > 0 ? "+" : "−"}${abs}°`;
}

export function formatDeclinationCompass(deg: number): string {
  const abs = Math.abs(deg).toFixed(1);
  if (Math.abs(deg) < 0.05) return "0.0°";
  return `${abs}° ${deg > 0 ? "E" : "W"}`;
}

export function formatSignedMilsNumber(mils: number): string {
  const rounded = Math.round(mils);
  if (rounded === 0) return "0";
  return `${rounded > 0 ? "+" : "−"}${Math.abs(rounded)}`;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const decimals = unitIndex === 0 ? 0 : value >= 100 ? 0 : 1;
  return `${value.toFixed(decimals)} ${units[unitIndex] ?? "B"}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const mins = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return `${mins}m ${rem.toString().padStart(2, "0")}s`;
}

const STAGE_LABELS: Record<string, string> = {
  queued: "Queued for worker",
  preparing: "Preparing sheet geometry",
  fetching_data: "Fetching DEM & OSM data",
  fetching_dem: "Streaming elevation DEM",
  fetching_osm: "Fetching OSM & overlays",
  terrain: "Generating contours & hillshade",
  building_terrain: "Generating contours & hillshade",
  viewshed: "Computing RF / LOS viewshed",
  rendering: "Rendering PyQGIS map sheet",
  rendering_pdf: "Rendering print PDF",
  rendering_supplements: "Generating RF Link / Route Card / Index",
  merging_atlas: "Merging multi-sheet Atlas PDF",
  exporting_mbtiles: "Exporting ATAK .mbtiles",
  exporting_kmz: "Exporting Garmin .kmz",
  exporting_gpkg: "Exporting GeoPackage .gpkg",
  exporting_osmand: "Exporting OsmAnd .sqlitedb",
  exporting_bundle: "Packaging Field Bundle .zip",
  finalizing: "Finalizing artifacts",
  done: "Completed",
  completed: "Completed",
  failed: "Render failed",
  cancelled: "Cancelled",
};

export function formatStage(stage: string): string {
  const trimmed = stage.trim();
  const key = trimmed.toLowerCase();
  if (STAGE_LABELS[key]) return STAGE_LABELS[key];
  const sheetMatch = /^rendering_sheet_([A-Z]\d+)(.*)$/i.exec(trimmed);
  if (sheetMatch) {
    return `Rendering sheet ${sheetMatch[1]?.toUpperCase() ?? ""}${sheetMatch[2] ?? ""}`;
  }
  return trimmed.replace(/_/g, " ");
}

export function formatArtifactLabel(kind: string, filename: string): string {
  const lowerKind = kind.toLowerCase();
  const lowerName = filename.toLowerCase();
  if (lowerKind.includes("geopdf")) return "GeoPDF";
  if (lowerKind.includes("pdf") || lowerName.endsWith(".pdf")) return "Print PDF";
  if (lowerKind.includes("mbtiles") || lowerName.endsWith(".mbtiles")) {
    return "ATAK .mbtiles";
  }
  if (lowerKind.includes("kmz") || lowerName.endsWith(".kmz")) {
    return "Garmin .kmz";
  }
  if (lowerKind.includes("gpkg") || lowerName.endsWith(".gpkg")) {
    return "GeoPackage .gpkg";
  }
  if (lowerKind.includes("osmand") || lowerKind.includes("sqlitedb") || lowerName.endsWith(".sqlitedb")) {
    return "OsmAnd .sqlitedb";
  }
  if (lowerKind.includes("bundle") || lowerKind.includes("zip") || lowerName.endsWith(".zip")) {
    return "Field Bundle .zip";
  }
  return filename;
}

function shellQuote(arg: string): string {
  if (/^[a-zA-Z0-9._/:=-]+$/.test(arg)) return arg;
  return "'" + arg.replace(/'/g, "'\\''") + "'";
}

function formatScaleK(scale: number): string {
  return scale % 1000 === 0 ? `${scale / 1000}k` : `${Number((scale / 1000).toFixed(1))}k`;
}

function slugifyFilename(state: AppFormState): string {
  const raw = state.title.trim() || `sheet_${formatScaleK(state.scale)}`;
  const slug = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  return `${slug || "gridforge_sheet"}_${state.paper}_${state.color_mode}`;
}

export type CliCopyResult = { command: string } | { error: string };

export function buildCliCommand(state: AppFormState): CliCopyResult {
  const pdfEnabled = state.outputs.pdf;
  const legacyDigital = state.outputs.mbtiles || state.outputs.kmz;
  const extraDigital =
    state.outputs.gpkg_tiles ||
    state.outputs.osmand_sqlitedb ||
    state.outputs.field_bundle_zip;
  const digital = legacyDigital || extraDigital;
  if (!pdfEnabled && !digital) {
    return { error: "Select at least one output to copy a command." };
  }

  const stripMode = Boolean(state.stripPlan && pdfEnabled);
  if (
    pdfEnabled &&
    !stripMode &&
    (state.grid_cols > 1 || state.grid_rows > 1)
  ) {
    return {
      error:
        "Use Generate Map Sheet for rectangular atlas PDFs, or select 1×1 to copy a CLI command.",
    };
  }
  if (stripMode && (!state.overlayFile || digital)) {
    return {
      error:
        "To copy a strip PDF command, select its route file and PDF as the only output. Studio can generate the combined job.",
    };
  }
  if (!pdfEnabled && extraDigital && !legacyDigital) {
    // Support single export format for gpkg/osmand/bundle via gridforge export, or error on mixed multi-extra without PDF
    const enabledExtras = [
      state.outputs.gpkg_tiles ? "gpkg" : null,
      state.outputs.osmand_sqlitedb ? "osmand" : null,
      state.outputs.field_bundle_zip ? "bundle" : null,
    ].filter((v): v is string => v !== null);
    if (enabledExtras.length > 1) {
      return {
        error:
          "Select Print PDF or a single export format to copy a CLI command, or use Generate Map Sheet.",
      };
    }
  } else if (!pdfEnabled && extraDigital && legacyDigital) {
    return {
      error:
        "Use Generate Map Sheet for combined device exports without Print PDF, or enable Print PDF to copy a CLI command.",
    };
  }

  for (const wp of state.waypoints) {
    const label = wp.label.trim();
    if (!label || label.includes(",")) {
      return {
        error:
          "Use a nonempty waypoint label without commas to copy a CLI command, or generate the map in Studio.",
      };
    }
  }

  const centerArg = state.center.trim() || formatCoords(state.lat, state.lon);
  const slug = slugifyFilename(state);
  const subcmd = pdfEnabled ? "render" : "export";

  const args: string[] = ["docker compose run --rm cli gridforge", subcmd];

  if (stripMode && state.overlayFile) {
    args.push(
      `--strip-route ${shellQuote(`/out/inputs/${state.overlayFile.name}`)}`
    );
  } else {
    args.push(`--center ${shellQuote(centerArg)}`);
  }

  args.push(
    `--scale ${state.scale}`,
    `--paper ${state.paper}`,
    `--orientation ${state.orientation}`,
    `--mode ${state.color_mode}`,
    `--units ${state.units}`
  );

  if (state.title.trim()) {
    args.push(`--title ${shellQuote(state.title.trim())}`);
  }
  if (state.dem_source !== "auto") {
    args.push(`--dem-source ${state.dem_source}`);
  }
  if (state.pack_id) {
    args.push(`--pack-id ${shellQuote(state.pack_id)}`);
  }
  for (const ov of state.au_overlays) {
    args.push(`--au-overlay ${ov}`);
  }
  for (const ov of state.nz_overlays) {
    args.push(`--nz-overlay ${ov}`);
  }
  for (const ov of state.ca_overlays) {
    args.push(`--ca-overlay ${ov}`);
  }
  for (const ov of state.us_overlays) {
    args.push(`--us-overlay ${ov}`);
  }

  if (state.carto.contour_density !== "normal") {
    args.push(`--contour-density ${state.carto.contour_density}`);
  }
  if (state.carto.hillshade_strength !== "normal") {
    args.push(`--hillshade-strength ${state.carto.hillshade_strength}`);
  }
  if (state.carto.label_density !== "normal") {
    args.push(`--label-density ${state.carto.label_density}`);
  }
  if (state.carto.show_slope_shading) args.push("--slope-shading");
  if (!state.carto.show_buildings) args.push("--no-buildings");
  if (!state.carto.show_trees) args.push("--no-trees");
  if (!state.carto.show_power) args.push("--no-power");
  if (!state.carto.show_pipelines) args.push("--no-pipelines");
  if (!state.carto.show_river_arrows) args.push("--no-river-arrows");
  if (pdfEnabled && !state.carto.show_grid) args.push("--no-grid");
  if (pdfEnabled && digital && !state.outputs.include_grid_on_digital) {
    args.push("--no-export-grid");
  }
  if (
    !pdfEnabled &&
    !(state.carto.show_grid && state.outputs.include_grid_on_digital)
  ) {
    args.push("--no-grid");
  }

  if (state.overlayFile && !stripMode) {
    args.push(
      `--overlay ${shellQuote(`/out/inputs/${state.overlayFile.name}`)}`
    );
  }

  for (const wp of state.waypoints) {
    const parts = [
      wp.label.trim(),
      String(wp.lat),
      String(wp.lon),
      wp.category,
    ];
    if (wp.notes && wp.notes.trim()) {
      parts.push(wp.notes.trim());
    }
    args.push(`--waypoint ${shellQuote(parts.join(","))}`);
  }

  for (const vs of state.viewsheds) {
    const o = vs.observer;
    const cleanLabel = o.label.replace(/,/g, " ").trim() || "Observer";
    const maxKm = Number((o.max_distance_m / 1000).toFixed(2));
    const vsSpec = `${o.lat.toFixed(5)},${o.lon.toFixed(5)},${o.observer_height_m},${o.target_height_m},${maxKm},${cleanLabel}`;
    args.push(`--viewshed ${shellQuote(vsSpec)}`);
  }

  for (const link of state.rfLinks) {
    const s = link.spec;
    const specStr = `${s.from_lat.toFixed(5)},${s.from_lon.toFixed(5)},${s.to_lat.toFixed(5)},${s.to_lon.toFixed(5)},${s.freq_mhz},${s.from_height_m},${s.to_height_m}`;
    args.push(`--rf-link ${shellQuote(specStr)}`);
  }

  for (const tg of state.tacticalGraphics) {
    if (tg.kind === "range_ring" && tg.points[0]) {
      const [lat, lon] = tg.points[0];
      const radii = tg.radii_km.join(":");
      const lbl = tg.label.replace(/,/g, " ").trim();
      const parts = [lat.toFixed(5), lon.toFixed(5), radii];
      if (lbl) parts.push(lbl);
      args.push(`--range-ring ${shellQuote(parts.join(","))}`);
    } else if (tg.kind === "bearing_line" && tg.points.length >= 2) {
      const p0 = tg.points[0]!;
      const p1 = tg.points[tg.points.length - 1]!;
      const lbl = tg.label.replace(/,/g, " ").trim();
      const parts = [
        p0[0].toFixed(5),
        p0[1].toFixed(5),
        p1[0].toFixed(5),
        p1[1].toFixed(5),
      ];
      if (lbl) parts.push(lbl);
      args.push(`--tactical-line ${shellQuote(parts.join(","))}`);
    }
  }

  if (pdfEnabled) {
    if (state.dpi !== 300) args.push(`--dpi ${state.dpi}`);
    if (digital && state.outputs.export_dpi !== state.dpi) {
      args.push(`--export-dpi ${state.outputs.export_dpi}`);
    }
    if (state.outputs.geopdf) args.push("--geopdf");
    if (state.show_fold_marks) args.push("--fold-marks");
    if (state.include_atlas_index) args.push("--atlas-index");
    if (state.include_calibration_page) args.push("--calibration-page");
    if (state.include_route_card) args.push("--route-card");
    if (state.include_elevation_profile) args.push("--elevation-profile");
    if (state.include_rf_link_card) args.push("--rf-link-card");
    if (state.outputs.mbtiles) {
      args.push(`--mbtiles /out/${slug}.mbtiles`);
      if (state.outputs.mbtiles_format !== "png") {
        args.push(`--mbtiles-format ${state.outputs.mbtiles_format}`);
      }
    }
    if (state.outputs.kmz) {
      args.push(`--kmz /out/${slug}.kmz`);
      if (state.outputs.garmin_profile !== "standard") {
        args.push(`--garmin-profile ${state.outputs.garmin_profile}`);
      }
    }
    if (state.outputs.gpkg_tiles) {
      args.push(`--gpkg /out/${slug}.gpkg`);
    }
    if (state.outputs.osmand_sqlitedb) {
      args.push(`--osmand /out/${slug}.sqlitedb`);
    }
    if (state.outputs.field_bundle_zip) {
      args.push(`--field-bundle /out/${slug}_field_bundle.zip`);
    }
    args.push(`--output /out/${slug}.pdf`);
  } else if (state.outputs.gpkg_tiles) {
    if (state.outputs.export_dpi !== 300) {
      args.push(`--dpi ${state.outputs.export_dpi}`);
    }
    args.push(`--format gpkg`, `--output /out/${slug}.gpkg`);
  } else if (state.outputs.osmand_sqlitedb) {
    if (state.outputs.export_dpi !== 300) {
      args.push(`--dpi ${state.outputs.export_dpi}`);
    }
    args.push(`--format osmand`, `--output /out/${slug}.sqlitedb`);
  } else if (state.outputs.field_bundle_zip) {
    if (state.outputs.export_dpi !== 300) {
      args.push(`--dpi ${state.outputs.export_dpi}`);
    }
    args.push(`--format bundle`, `--output /out/${slug}_field_bundle.zip`);
  } else {
    if (state.outputs.export_dpi !== 300) {
      args.push(`--dpi ${state.outputs.export_dpi}`);
    }
    const fmt =
      state.outputs.mbtiles && state.outputs.kmz
        ? "both"
        : state.outputs.mbtiles
          ? "mbtiles"
          : "kmz";
    args.push(`--format ${fmt}`);
    if (state.outputs.mbtiles && state.outputs.mbtiles_format !== "png") {
      args.push(`--mbtiles-format ${state.outputs.mbtiles_format}`);
    }
    if (state.outputs.kmz && state.outputs.garmin_profile !== "standard") {
      args.push(`--garmin-profile ${state.outputs.garmin_profile}`);
    }
    const ext = fmt === "both" ? "" : `.${fmt}`;
    args.push(`--output /out/${slug}${ext}`);
  }

  return { command: args.join(" ") };
}

export function escapeHtml(input: string): string {
  return input
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
