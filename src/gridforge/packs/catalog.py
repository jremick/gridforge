"""Curated Australian state/territory and tactical region catalog for offline packs."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel

from gridforge.config import BBox
from gridforge.geo.crs import MAX_UTM_LAT, MIN_UTM_LAT, resolve_utm_zone


class RegionPreset(BaseModel):
    """Named bounding-box preset for building an air-gapped regional pack."""

    region_id: str
    name: str
    category: Literal["state", "tactical", "custom"]
    bbox_wgs84: BBox
    utm_zones: list[str]
    description: str = ""
    estimated_size_hint: str = ""


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
    category: Literal["state", "tactical", "custom"],
    bbox: BBox,
    description: str,
    estimated_size_hint: str,
) -> RegionPreset:
    return RegionPreset(
        region_id=region_id,
        name=name,
        category=category,
        bbox_wgs84=bbox,
        utm_zones=compute_utm_zones_for_bbox(bbox),
        description=description,
        estimated_size_hint=estimated_size_hint,
    )


_RAW_REGIONS: tuple[RegionPreset, ...] = (
    # --- 8 Australian States & Territories ---
    _preset(
        "act",
        "Australian Capital Territory",
        "state",
        BBox(min_x=148.74, min_y=-35.94, max_x=149.42, max_y=-35.10),
        "Canberra, Namadgi National Park, and Brindabella Range.",
        "~45 MB",
    ),
    _preset(
        "tas",
        "Tasmania",
        "state",
        BBox(min_x=143.80, min_y=-43.75, max_x=148.55, max_y=-39.55),
        "Full Tasmania mainland, Bass Strait islands, and alpine highlands.",
        "~220 MB",
    ),
    _preset(
        "vic",
        "Victoria",
        "state",
        BBox(min_x=140.95, min_y=-39.20, max_x=150.05, max_y=-33.95),
        "Full Victoria from Mallee & Grampians to Victorian Alps and Gippsland.",
        "~650 MB",
    ),
    _preset(
        "nsw",
        "New South Wales",
        "state",
        BBox(min_x=140.95, min_y=-37.55, max_x=153.68, max_y=-28.15),
        "Full New South Wales including Sydney, Blue Mountains, Snowy Mtns & Outback.",
        "~1.4 GB",
    ),
    _preset(
        "sa",
        "South Australia",
        "state",
        BBox(min_x=128.98, min_y=-38.10, max_x=141.05, max_y=-25.95),
        "Adelaide Hills, Flinders Ranges, Eyre Peninsula, and arid interior.",
        "~900 MB",
    ),
    _preset(
        "qld",
        "Queensland",
        "state",
        BBox(min_x=137.95, min_y=-29.20, max_x=153.60, max_y=-10.00),
        "Full Queensland from Southeast QLD to Cape York & Channel Country.",
        "~2.1 GB",
    ),
    _preset(
        "nt",
        "Northern Territory",
        "state",
        BBox(min_x=128.98, min_y=-26.05, max_x=138.05, max_y=-10.90),
        "Top End, Kakadu, Arnhem Land, Barkly Tableland, and Red Centre.",
        "~1.2 GB",
    ),
    _preset(
        "wa",
        "Western Australia",
        "state",
        BBox(min_x=112.85, min_y=-35.20, max_x=129.05, max_y=-13.65),
        "Perth & Southwest, Pilbara, Kimberley, and Great Victoria Desert.",
        "~2.8 GB",
    ),
    # --- 7 Compact High-Value Tactical Regions ---
    _preset(
        "blue-mountains",
        "Blue Mountains (NSW)",
        "tactical",
        BBox(min_x=150.00, min_y=-34.05, max_x=150.70, max_y=-33.40),
        "Katoomba, Jamison & Grose Valleys, Wollemi & Kanangra-Boyd escarpments.",
        "~35 MB",
    ),
    _preset(
        "sydney-basin",
        "Sydney Basin (NSW)",
        "tactical",
        BBox(min_x=150.55, min_y=-34.25, max_x=151.40, max_y=-33.45),
        "Sydney Harbour, Ku-ring-gai, Royal National Park, and Hawkesbury.",
        "~90 MB",
    ),
    _preset(
        "australian-alps",
        "Australian Alps / Kosciuszko (NSW/VIC)",
        "tactical",
        BBox(min_x=147.80, min_y=-37.10, max_x=149.10, max_y=-35.80),
        "Mt Kosciuszko, Main Range, Jagungal, and Alpine National Park.",
        "~65 MB",
    ),
    _preset(
        "tasmania-wilderness",
        "Tasmanian Wilderness / Cradle Mtn (TAS)",
        "tactical",
        BBox(min_x=145.40, min_y=-42.50, max_x=146.60, max_y=-41.30),
        "Cradle Mountain-Lake St Clair, Walls of Jerusalem, and Overland Track.",
        "~55 MB",
    ),
    _preset(
        "red-centre",
        "Red Centre / Uluru–MacDonnell Ranges (NT)",
        "tactical",
        BBox(min_x=130.40, min_y=-25.60, max_x=134.20, max_y=-23.40),
        "Uluru-Kata Tjuta, Kings Canyon, West MacDonnell Ranges & Alice Springs.",
        "~85 MB",
    ),
    _preset(
        "perth-hills",
        "Perth & Darling Scarp (WA)",
        "tactical",
        BBox(min_x=115.65, min_y=-32.35, max_x=116.45, max_y=-31.55),
        "Perth metropolitan area, Swan River, Mundaring, and Darling Range.",
        "~60 MB",
    ),
    _preset(
        "far-north-qld",
        "Far North QLD / Daintree & Atherton (QLD)",
        "tactical",
        BBox(min_x=145.00, min_y=-17.60, max_x=146.10, max_y=-15.80),
        "Cairns, Atherton Tablelands, Bartle Frere, and Daintree Rainforest.",
        "~75 MB",
    ),
)

AUSTRALIA_REGIONS: dict[str, RegionPreset] = {r.region_id: r for r in _RAW_REGIONS}


def list_regions() -> list[RegionPreset]:
    """Return all curated Australian state/territory and tactical region presets."""
    return list(_RAW_REGIONS)


def resolve_region(region_or_bbox: str) -> RegionPreset:
    """Resolve a catalog region ID (e.g. 'tas', 'blue-mountains') or custom 'w,s,e,n' bbox."""
    raw = region_or_bbox.strip()
    if not raw:
        raise ValueError("Region identifier or 'w,s,e,n' bounding box must be non-empty.")

    key = re.sub(r"[\s_]+", "-", raw.lower())
    if key in AUSTRALIA_REGIONS:
        return AUSTRALIA_REGIONS[key]

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

        bbox = BBox(min_x=w, min_y=s, max_x=e, max_y=n)
        slug = re.sub(r"[^a-z0-9]+", "-", f"custom-{w:.2f}-{s:.2f}-{e:.2f}-{n:.2f}").strip("-")
        return RegionPreset(
            region_id=slug,
            name=f"Custom ({w:.3f}, {s:.3f}, {e:.3f}, {n:.3f})",
            category="custom",
            bbox_wgs84=bbox,
            utm_zones=compute_utm_zones_for_bbox(bbox),
            description="User-defined WGS84 bounding box pack.",
        )

    available = ", ".join(AUSTRALIA_REGIONS.keys())
    raise ValueError(
        f"Unknown region '{region_or_bbox}'. Use 'w,s,e,n' or a catalog ID: {available}."
    )
