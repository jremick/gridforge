"""Curated 29-region global offline pack catalog (AU Tactical, AU States, NZ, North America)."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel

from gridforge.config import BBox
from gridforge.geo.crs import MAX_UTM_LAT, MIN_UTM_LAT, resolve_utm_zone

DEFAULT_AU_PBF_URL: str = (
    "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf"
)
DEFAULT_NZ_PBF_URL: str = (
    "https://download.geofabrik.de/australia-oceania/new-zealand-latest.osm.pbf"
)

RegionGroup = Literal["au_tactical", "au_state", "nz", "na", "custom"]
RegionCategory = Literal["state", "tactical", "custom"]
RegionCountry = Literal["AU", "NZ", "US", "CA", "CUSTOM"]
PackDemTier = Literal["auto", "srtm-1s", "glo30", "au-elvis", "nz-linz", "usgs3dep", "ca-hrdem"]

REGION_GROUPS: tuple[RegionGroup, ...] = ("au_tactical", "au_state", "nz", "na")


class RegionPreset(BaseModel):
    """Named bounding-box preset for building an air-gapped regional pack."""

    region_id: str
    name: str
    group: RegionGroup = "au_tactical"
    category: RegionCategory
    country: RegionCountry = "AU"
    bbox_wgs84: BBox
    utm_zones: list[str]
    description: str = ""
    estimated_size_hint: str = ""
    default_dem_tier: PackDemTier = "srtm-1s"
    pbf_url: str = DEFAULT_AU_PBF_URL


def compute_utm_zones_for_bbox(bbox: BBox) -> list[str]:
    """Return sorted distinct MGRS Grid Zone Designations intersecting a WGS84 BBox."""
    zones: set[str] = set()
    # Sample at 1-degree steps (plus exact boundaries) to catch every zone & band.
    lon_steps = max(2, int(math.ceil(bbox.width / 1.0)) + 1)
    lat_steps = max(2, int(math.ceil(bbox.height / 2.0)) + 1)

    for ix in range(lon_steps):
        lon = bbox.min_x + (bbox.width * ix / (lon_steps - 1))
        lon = max(-180.0, min(180.0, lon))
        for iy in range(lat_steps):
            lat = bbox.min_y + (bbox.height * iy / (lat_steps - 1))
            lat = max(MIN_UTM_LAT, min(MAX_UTM_LAT, lat))
            _, _, _, gzd = resolve_utm_zone(lat, lon)
            zones.add(gzd)

    def _gzd_key(gzd: str) -> tuple[int, str]:
        return (int(gzd[:-1]), gzd[-1])

    return sorted(zones, key=_gzd_key)


def _preset(
    region_id: str,
    name: str,
    *,
    group: RegionGroup,
    category: RegionCategory,
    country: RegionCountry,
    bbox: BBox,
    description: str,
    estimated_size_hint: str,
    default_dem_tier: PackDemTier,
    pbf_url: str,
) -> RegionPreset:
    return RegionPreset(
        region_id=region_id,
        name=name,
        group=group,
        category=category,
        country=country,
        bbox_wgs84=bbox,
        utm_zones=compute_utm_zones_for_bbox(bbox),
        description=description,
        estimated_size_hint=estimated_size_hint,
        default_dem_tier=default_dem_tier,
        pbf_url=pbf_url,
    )


_AU_STATE_REGIONS: tuple[RegionPreset, ...] = (
    _preset(
        "act",
        "Australian Capital Territory",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=148.74, min_y=-35.94, max_x=149.42, max_y=-35.10),
        description="Canberra, Namadgi National Park, and Brindabella Range.",
        estimated_size_hint="~45 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "tas",
        "Tasmania",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=143.80, min_y=-43.75, max_x=148.55, max_y=-39.55),
        description="Full Tasmania mainland, Bass Strait islands, and alpine highlands.",
        estimated_size_hint="~220 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "vic",
        "Victoria",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=140.95, min_y=-39.20, max_x=150.05, max_y=-33.95),
        description="Full Victoria from Mallee & Grampians to Victorian Alps and Gippsland.",
        estimated_size_hint="~650 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "nsw",
        "New South Wales",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=140.95, min_y=-37.55, max_x=153.68, max_y=-28.15),
        description="Full New South Wales including Sydney, Blue Mountains, Snowy Mtns & Outback.",
        estimated_size_hint="~1.4 GB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "sa",
        "South Australia",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=128.98, min_y=-38.10, max_x=141.05, max_y=-25.95),
        description="Adelaide Hills, Flinders Ranges, Eyre Peninsula, and arid interior.",
        estimated_size_hint="~900 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "qld",
        "Queensland",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=137.95, min_y=-29.20, max_x=153.60, max_y=-10.00),
        description="Full Queensland from Southeast QLD to Cape York & Channel Country.",
        estimated_size_hint="~2.1 GB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "nt",
        "Northern Territory",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=128.98, min_y=-26.05, max_x=138.05, max_y=-10.90),
        description="Top End, Kakadu, Arnhem Land, Barkly Tableland, and Red Centre.",
        estimated_size_hint="~1.2 GB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "wa",
        "Western Australia",
        group="au_state",
        category="state",
        country="AU",
        bbox=BBox(min_x=112.85, min_y=-35.20, max_x=129.05, max_y=-13.65),
        description="Perth & Southwest, Pilbara, Kimberley, and Great Victoria Desert.",
        estimated_size_hint="~2.8 GB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
)

_AU_TACTICAL_REGIONS: tuple[RegionPreset, ...] = (
    _preset(
        "blue-mountains",
        "Blue Mountains (NSW)",
        group="au_tactical",
        category="tactical",
        country="AU",
        bbox=BBox(min_x=150.00, min_y=-34.05, max_x=150.70, max_y=-33.40),
        description="Katoomba, Jamison & Grose Valleys, Wollemi & Kanangra-Boyd escarpments.",
        estimated_size_hint="~35 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "sydney-basin",
        "Sydney Basin (NSW)",
        group="au_tactical",
        category="tactical",
        country="AU",
        bbox=BBox(min_x=150.55, min_y=-34.25, max_x=151.40, max_y=-33.45),
        description="Sydney Harbour, Ku-ring-gai, Royal National Park, and Hawkesbury.",
        estimated_size_hint="~90 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "australian-alps",
        "Australian Alps / Kosciuszko (NSW/VIC)",
        group="au_tactical",
        category="tactical",
        country="AU",
        bbox=BBox(min_x=147.80, min_y=-37.10, max_x=149.10, max_y=-35.80),
        description="Mt Kosciuszko, Main Range, Jagungal, and Alpine National Park.",
        estimated_size_hint="~65 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "tasmania-wilderness",
        "Tasmanian Wilderness / Cradle Mtn (TAS)",
        group="au_tactical",
        category="tactical",
        country="AU",
        bbox=BBox(min_x=145.40, min_y=-42.50, max_x=146.60, max_y=-41.30),
        description="Cradle Mountain-Lake St Clair, Walls of Jerusalem, and Overland Track.",
        estimated_size_hint="~55 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "red-centre",
        "Red Centre / Uluru–MacDonnell Ranges (NT)",
        group="au_tactical",
        category="tactical",
        country="AU",
        bbox=BBox(min_x=130.40, min_y=-25.60, max_x=134.20, max_y=-23.40),
        description="Uluru-Kata Tjuta, Kings Canyon, West MacDonnell Ranges & Alice Springs.",
        estimated_size_hint="~85 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "perth-hills",
        "Perth & Darling Scarp (WA)",
        group="au_tactical",
        category="tactical",
        country="AU",
        bbox=BBox(min_x=115.65, min_y=-32.35, max_x=116.45, max_y=-31.55),
        description="Perth metropolitan area, Swan River, Mundaring, and Darling Range.",
        estimated_size_hint="~60 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
    _preset(
        "far-north-qld",
        "Far North QLD / Daintree & Atherton (QLD)",
        group="au_tactical",
        category="tactical",
        country="AU",
        bbox=BBox(min_x=145.00, min_y=-17.60, max_x=146.10, max_y=-15.80),
        description="Cairns, Atherton Tablelands, Bartle Frere, and Daintree Rainforest.",
        estimated_size_hint="~75 MB",
        default_dem_tier="srtm-1s",
        pbf_url=DEFAULT_AU_PBF_URL,
    ),
)

_NZ_REGIONS: tuple[RegionPreset, ...] = (
    _preset(
        "nz-tongariro",
        "Tongariro & Central Plateau (NZ)",
        group="nz",
        category="tactical",
        country="NZ",
        bbox=BBox(min_x=175.35, min_y=-39.45, max_x=176.00, max_y=-38.90),
        description="Tongariro Alpine Crossing, Mt Ngāuruhoe, Mt Ruapehu, and Kaimanawa Range.",
        estimated_size_hint="~55 MB",
        default_dem_tier="nz-linz",
        pbf_url=DEFAULT_NZ_PBF_URL,
    ),
    _preset(
        "nz-aoraki-mt-cook",
        "Aoraki / Mt Cook & Southern Alps (NZ)",
        group="nz",
        category="tactical",
        country="NZ",
        bbox=BBox(min_x=169.70, min_y=-44.00, max_x=170.55, max_y=-43.40),
        description="Aoraki / Mt Cook National Park, Hooker & Tasman Valleys, and Westland glaciers.",
        estimated_size_hint="~65 MB",
        default_dem_tier="nz-linz",
        pbf_url=DEFAULT_NZ_PBF_URL,
    ),
    _preset(
        "nz-fiordland",
        "Fiordland / Milford & Routeburn (NZ)",
        group="nz",
        category="tactical",
        country="NZ",
        bbox=BBox(min_x=167.30, min_y=-45.65, max_x=168.45, max_y=-44.55),
        description="Milford Sound, Milford, Routeburn & Kepler Great Walks, and Te Anau basin.",
        estimated_size_hint="~95 MB",
        default_dem_tier="nz-linz",
        pbf_url=DEFAULT_NZ_PBF_URL,
    ),
    _preset(
        "nz-abel-tasman",
        "Abel Tasman & Kahurangi (NZ)",
        group="nz",
        category="tactical",
        country="NZ",
        bbox=BBox(min_x=172.30, min_y=-41.35, max_x=173.20, max_y=-40.70),
        description="Abel Tasman Coast Track, Heaphy Track, Mt Arthur, and Golden Bay.",
        estimated_size_hint="~60 MB",
        default_dem_tier="nz-linz",
        pbf_url=DEFAULT_NZ_PBF_URL,
    ),
    _preset(
        "nz-arthurs-pass",
        "Arthur's Pass & Craigieburn (NZ)",
        group="nz",
        category="tactical",
        country="NZ",
        bbox=BBox(min_x=171.25, min_y=-43.30, max_x=172.05, max_y=-42.75),
        description="Arthur's Pass National Park, Avalanche Peak, Waimakariri & Mingha-Deception.",
        estimated_size_hint="~55 MB",
        default_dem_tier="nz-linz",
        pbf_url=DEFAULT_NZ_PBF_URL,
    ),
    _preset(
        "nz-taranaki",
        "Mt Taranaki / Egmont (NZ)",
        group="nz",
        category="tactical",
        country="NZ",
        bbox=BBox(min_x=173.85, min_y=-39.50, max_x=174.30, max_y=-39.12),
        description="Te Papakura o Taranaki, Pouakai Circuit, Dawson Falls, and summit routes.",
        estimated_size_hint="~35 MB",
        default_dem_tier="nz-linz",
        pbf_url=DEFAULT_NZ_PBF_URL,
    ),
)

_NA_REGIONS: tuple[RegionPreset, ...] = (
    _preset(
        "us-colorado-front-range",
        "Colorado Front Range / Rocky Mtn NP (US)",
        group="na",
        category="tactical",
        country="US",
        bbox=BBox(min_x=-106.05, min_y=39.55, max_x=-105.15, max_y=40.50),
        description="Boulder, Indian Peaks, Longs Peak, and Rocky Mountain National Park.",
        estimated_size_hint="~90 MB",
        default_dem_tier="usgs3dep",
        pbf_url="https://download.geofabrik.de/north-america/us/colorado-latest.osm.pbf",
    ),
    _preset(
        "us-yosemite-sierra",
        "Yosemite & High Sierra (US)",
        group="na",
        category="tactical",
        country="US",
        bbox=BBox(min_x=-119.95, min_y=37.45, max_x=-119.05, max_y=38.25),
        description="Yosemite Valley, Tuolumne Meadows, Ansel Adams Wilderness, and John Muir Trail.",
        estimated_size_hint="~85 MB",
        default_dem_tier="usgs3dep",
        pbf_url="https://download.geofabrik.de/north-america/us/california-latest.osm.pbf",
    ),
    _preset(
        "us-cascades-rainier",
        "Mt Rainier & Central Cascades (US)",
        group="na",
        category="tactical",
        country="US",
        bbox=BBox(min_x=-122.15, min_y=46.60, max_x=-121.30, max_y=47.20),
        description="Mt Rainier National Park, Wonderland Trail, Goat Rocks, and Crystal Mountain.",
        estimated_size_hint="~80 MB",
        default_dem_tier="usgs3dep",
        pbf_url="https://download.geofabrik.de/north-america/us/washington-latest.osm.pbf",
    ),
    _preset(
        "us-white-mountains",
        "White Mountains / Presidential Range (US)",
        group="na",
        category="tactical",
        country="US",
        bbox=BBox(min_x=-71.80, min_y=43.90, max_x=-71.00, max_y=44.50),
        description="Mt Washington, Franconia Ridge, Pemigewasset Wilderness, and AMC huts.",
        estimated_size_hint="~70 MB",
        default_dem_tier="usgs3dep",
        pbf_url="https://download.geofabrik.de/north-america/us/new-hampshire-latest.osm.pbf",
    ),
    _preset(
        "us-utah-moab",
        "Moab, Canyonlands & La Sal Mtns (US)",
        group="na",
        category="tactical",
        country="US",
        bbox=BBox(min_x=-110.05, min_y=38.10, max_x=-109.10, max_y=38.90),
        description="Arches, Canyonlands Island in the Sky, Needles, and La Sal alpine peaks.",
        estimated_size_hint="~65 MB",
        default_dem_tier="usgs3dep",
        pbf_url="https://download.geofabrik.de/north-america/us/utah-latest.osm.pbf",
    ),
    _preset(
        "ca-banff-rockies",
        "Banff & Lake Louise Rockies (CA)",
        group="na",
        category="tactical",
        country="CA",
        bbox=BBox(min_x=-116.55, min_y=50.95, max_x=-115.25, max_y=51.70),
        description="Banff, Lake Louise, Yoho, Kananaskis, and Continental Divide alpine terrain.",
        estimated_size_hint="~95 MB",
        default_dem_tier="ca-hrdem",
        pbf_url="https://download.geofabrik.de/north-america/canada/alberta-latest.osm.pbf",
    ),
    _preset(
        "ca-squamish-whistler",
        "Squamish, Garibaldi & Whistler (CA)",
        group="na",
        category="tactical",
        country="CA",
        bbox=BBox(min_x=-123.45, min_y=49.55, max_x=-122.55, max_y=50.30),
        description="Sea-to-Sky corridor, Garibaldi Provincial Park, Spearhead Traverse & Coast Mtns.",
        estimated_size_hint="~85 MB",
        default_dem_tier="ca-hrdem",
        pbf_url="https://download.geofabrik.de/north-america/canada/british-columbia-latest.osm.pbf",
    ),
    _preset(
        "ca-algonquin",
        "Algonquin Highlands (Ontario, CA)",
        group="na",
        category="tactical",
        country="CA",
        bbox=BBox(min_x=-79.10, min_y=45.30, max_x=-78.00, max_y=46.05),
        description="Algonquin Provincial Park backcountry canoe routes, portages, and ridge trails.",
        estimated_size_hint="~70 MB",
        default_dem_tier="ca-hrdem",
        pbf_url="https://download.geofabrik.de/north-america/canada/ontario-latest.osm.pbf",
    ),
)

_RAW_REGIONS: tuple[RegionPreset, ...] = (
    *_AU_STATE_REGIONS,
    *_AU_TACTICAL_REGIONS,
    *_NZ_REGIONS,
    *_NA_REGIONS,
)

AUSTRALIA_REGIONS: dict[str, RegionPreset] = {
    r.region_id: r for r in (*_AU_STATE_REGIONS, *_AU_TACTICAL_REGIONS)
}
ALL_REGIONS: dict[str, RegionPreset] = {r.region_id: r for r in _RAW_REGIONS}


MAX_CUSTOM_PACK_SPAN_DEG: float = 4.0


def _infer_custom_country(bbox: BBox) -> tuple[RegionCountry, PackDemTier, str]:
    c_lon = (bbox.min_x + bbox.max_x) / 2.0
    c_lat = (bbox.min_y + bbox.max_y) / 2.0
    if 112.0 <= c_lon <= 155.0 and -45.0 <= c_lat <= -9.5:
        return ("AU", "srtm-1s", DEFAULT_AU_PBF_URL)
    if 165.0 <= c_lon <= 179.5 and -48.0 <= c_lat <= -33.5:
        return ("NZ", "nz-linz", DEFAULT_NZ_PBF_URL)
    if -142.0 <= c_lon <= -52.0 and 49.0 <= c_lat <= 72.0:
        return ("CA", "ca-hrdem", "https://download.geofabrik.de/north-america/canada-latest.osm.pbf")
    if -126.0 <= c_lon <= -66.0 and 24.0 <= c_lat <= 49.5:
        return ("US", "usgs3dep", "https://download.geofabrik.de/north-america/us-latest.osm.pbf")
    return ("CUSTOM", "glo30", "")


def list_regions(group: RegionGroup | None = None) -> list[RegionPreset]:
    """Return all 29 curated offline pack presets, optionally filtered by ``group``."""
    if group is None:
        return list(_RAW_REGIONS)
    return [r for r in _RAW_REGIONS if r.group == group]


def resolve_region(region_or_bbox: str) -> RegionPreset:
    """Resolve a catalog region ID (e.g. 'tas', 'nz-tongariro', 'ca-banff-rockies') or custom 'w,s,e,n' bbox."""
    raw = region_or_bbox.strip()
    if not raw:
        raise ValueError("Region identifier or 'w,s,e,n' bounding box must be non-empty.")

    key = re.sub(r"[\s_]+", "-", raw.lower())
    if key in ALL_REGIONS:
        return ALL_REGIONS[key]

    parts = [p.strip() for p in raw.split(",")]
    if len(parts) == 4:
        try:
            w, s, e, n = (float(p) for p in parts)
        except ValueError as exc:
            raise ValueError(
                f"Invalid custom bounding box '{region_or_bbox}'. Expected 'w,s,e,n' floats."
            ) from exc

        if not (-180.0 <= w < e <= 180.0):
            raise ValueError(
                f"Invalid longitude bounds west={w}, east={e}; require -180 <= w < e <= 180."
            )
        if not (MIN_UTM_LAT <= s < n <= MAX_UTM_LAT):
            raise ValueError(
                f"Invalid latitude bounds south={s}, north={n}; "
                f"require {MIN_UTM_LAT} <= s < n <= {MAX_UTM_LAT}."
            )

        if (e - w) > MAX_CUSTOM_PACK_SPAN_DEG or (n - s) > MAX_CUSTOM_PACK_SPAN_DEG:
            raise ValueError(
                f"Custom pack bounding box span ({e - w:.2f}° x {n - s:.2f}°) exceeds "
                f"maximum {MAX_CUSTOM_PACK_SPAN_DEG:.1f}° x {MAX_CUSTOM_PACK_SPAN_DEG:.1f}° limit."
            )

        bbox = BBox(min_x=w, min_y=s, max_x=e, max_y=n)
        country, dem_tier, pbf_url = _infer_custom_country(bbox)
        slug = re.sub(r"[^a-z0-9]+", "-", f"custom-{w:.2f}-{s:.2f}-{e:.2f}-{n:.2f}").strip("-")
        return RegionPreset(
            region_id=slug,
            name=f"Custom ({w:.3f}, {s:.3f}, {e:.3f}, {n:.3f})",
            group="custom",
            category="custom",
            country=country,
            bbox_wgs84=bbox,
            utm_zones=compute_utm_zones_for_bbox(bbox),
            description="User-defined WGS84 bounding box pack.",
            default_dem_tier=dem_tier,
            pbf_url=pbf_url,
        )

    available = ", ".join(ALL_REGIONS.keys())
    raise ValueError(
        f"Unknown region '{region_or_bbox}'. Use 'w,s,e,n' or a catalog ID: {available}."
    )
