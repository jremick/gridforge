"""Machine-readable AI & client capability catalog for GridForge (zero QGIS imports)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from gridforge import __version__
from gridforge.config import (
    PAPER_DIMENSIONS_MM,
    VALID_SCALES,
    AUOverlayKind,
    CAOverlayKind,
    ColorMode,
    DemSourceKind,
    NZOverlayKind,
    Orientation,
    PaperSize,
    Units,
    USOverlayKind,
    WaypointCategory,
)
from gridforge.web.schemas import AuthCapabilities, AuthRole


class PaperSizeCapability(BaseModel):
    """Physical print paper size option."""

    id: str
    label: str
    width_mm: float
    height_mm: float


class ColorModeCapability(BaseModel):
    """Dedicated cartography theme description."""

    id: str
    label: str
    description: str


class DemSourceCapability(BaseModel):
    """Open elevation DEM source metadata."""

    id: str
    name: str
    region: str
    vertical_datum: str
    resolution: str
    license: str
    description: str


class OverlayCapability(BaseModel):
    """Optional regional open-data thematic overlay metadata."""

    id: str
    name: str
    license: str
    description: str


class OutputFormatCapability(BaseModel):
    """Supported print or offline digital export format."""

    id: str
    name: str
    extension: str
    description: str
    allowed_for_caller: bool = True


class RFBandPreset(BaseModel):
    """Common backcountry/tactical RF band frequency preset."""

    id: str
    label: str
    freq_mhz: float
    region: str
    description: str


class TacticalGraphicCapability(BaseModel):
    """Supported tactical markup geometry."""

    id: str
    name: str
    description: str
    required_fields: list[str] = Field(default_factory=list)


class CapabilitiesEndpoints(BaseModel):
    """Canonical API & AI discovery URLs."""

    mcp: str = "/mcp"
    openapi: str = "/openapi.json"
    docs: str = "/docs"
    llms_txt: str = "/llms.txt"
    capabilities: str = "/api/capabilities"
    health: str = "/api/health"
    geocode: str = "/api/geocode"
    footprint: str = "/api/footprint"
    rf_link_profile: str = "/api/rf-link-profile"
    viewshed_preview: str = "/api/viewshed-preview"
    route_metrics: str = "/api/route-metrics"
    ruler_metrics: str = "/api/ruler-metrics"
    strip_plan: str = "/api/strip-plan"
    jobs: str = "/api/jobs"
    shares: str = "/api/shares"
    packs: str = "/api/packs"
    api_keys: str = "/api/auth/api-keys"


class CapabilitiesResponse(BaseModel):
    """Response envelope for ``GET /api/capabilities``."""

    service: str = "gridforge"
    brand_name: str = "GridForge"
    version: str = __version__
    auth_mode: Literal["none", "hosted"] = "none"
    caller_role: AuthRole = "none"
    authenticated: bool = False
    public_url: str | None = None
    mcp_endpoint: str = "/mcp"
    openapi_url: str = "/openapi.json"
    llms_txt_url: str = "/llms.txt"
    endpoints: CapabilitiesEndpoints = Field(default_factory=CapabilitiesEndpoints)
    scales: list[int] = Field(default_factory=list)
    paper_sizes: list[PaperSizeCapability] = Field(default_factory=list)
    orientations: list[str] = Field(default_factory=list)
    color_modes: list[ColorModeCapability] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)
    dem_sources: list[DemSourceCapability] = Field(default_factory=list)
    regional_overlays: dict[str, list[OverlayCapability]] = Field(default_factory=dict)
    output_formats: list[OutputFormatCapability] = Field(default_factory=list)
    rf_band_presets: list[RFBandPreset] = Field(default_factory=list)
    tactical_graphics: list[TacticalGraphicCapability] = Field(default_factory=list)
    waypoint_categories: list[str] = Field(default_factory=list)
    installed_packs_count: int = 0
    signed_url_ttl_seconds: int = 3600
    caller_capabilities: AuthCapabilities
    notes: dict[str, Any] = Field(default_factory=dict)


_PAPER_LABELS: dict[PaperSize, str] = {
    PaperSize.LETTER: "US Letter (8.5 × 11 in)",
    PaperSize.A4: "ISO A4 (210 × 297 mm)",
    PaperSize.LEGAL: "US Legal (8.5 × 14 in)",
    PaperSize.A3: "ISO A3 (297 × 420 mm)",
}

_COLOR_MODE_SPECS: dict[ColorMode, tuple[str, str]] = {
    ColorMode.COLOR: (
        "Color (NATO / USGS Topographic)",
        "Full-color topographic palette with pastel forest tints, brown elevation contours, cyan hydrography, and high-contrast operational accents.",
    ),
    ColorMode.BW: (
        "Laser-Print B&W",
        "High-contrast monochrome vector-pattern cartography engineered for crisp 300–600 DPI office laser printing.",
    ),
    ColorMode.NIGHT_RED: (
        "Night-Red NVG",
        "Low-luminance red-light-compatible tactical palette preserving dark adaptation under red headlamp or NVG use.",
    ),
    ColorMode.ALPINE: (
        "Alpine / Winter",
        "Crisp snow-and-ice topographic palette highlighting steep terrain, glacier features, rock contours, and avalanche slope bands.",
    ),
}

_DEM_SOURCE_SPECS: dict[DemSourceKind, dict[str, str]] = {
    DemSourceKind.AUTO: {
        "name": "Auto Regional High-Res DEM",
        "region": "Global (auto-routes US / CA / AU / NZ / Global)",
        "vertical_datum": "Regional (NAVD88 / CGVD2013 / AHD / NZVD2016 / EGM2008)",
        "resolution": "1–30 m",
        "license": "Open Data (Public Domain / CC BY 4.0 / OGL-Canada / Copernicus)",
        "description": "Automatically chooses the highest-resolution open DEM covering the sheet coordinates, falling back to global Copernicus GLO-30.",
    },
    DemSourceKind.GLO30: {
        "name": "Copernicus GLO-30",
        "region": "Global",
        "vertical_datum": "EGM2008",
        "resolution": "30 m",
        "license": "Copernicus DEM License (Free & Open)",
        "description": "Global 30 m digital elevation model suitable for any UTM region worldwide.",
    },
    DemSourceKind.USGS3DEP: {
        "name": "USGS 3DEP",
        "region": "United States",
        "vertical_datum": "NAVD88",
        "resolution": "1–10 m",
        "license": "Public Domain (US Government)",
        "description": "High-resolution USGS 3D Elevation Program terrain across CONUS, Alaska, Hawaii, and territories.",
    },
    DemSourceKind.AU_ELVIS: {
        "name": "Geoscience Australia ELVIS",
        "region": "Australia",
        "vertical_datum": "AHD",
        "resolution": "5 m",
        "license": "CC BY 4.0",
        "description": "National 5 m LiDAR/photogrammetry DEM covering Australian backcountry and alpine ranges.",
    },
    DemSourceKind.NZ_LINZ: {
        "name": "LINZ New Zealand DEM",
        "region": "New Zealand",
        "vertical_datum": "NZVD2016",
        "resolution": "8 m",
        "license": "CC BY 4.0",
        "description": "Land Information New Zealand 8 m digital elevation model for North, South, and Stewart Islands.",
    },
    DemSourceKind.CA_HRDEM: {
        "name": "NRCan HRDEM",
        "region": "Canada",
        "vertical_datum": "CGVD2013",
        "resolution": "1–2 m",
        "license": "OGL-Canada",
        "description": "Natural Resources Canada High-Resolution Digital Elevation Model via STAC COGs.",
    },
}

_US_OVERLAY_SPECS: dict[USOverlayKind, tuple[str, str, str]] = {
    USOverlayKind.FEMA: ("FEMA Flood Hazard", "Public Domain", "FEMA National Flood Hazard Layer (100-year and floodway zones)."),
    USOverlayKind.NHD: ("USGS NHD Hydrography", "Public Domain", "National Hydrography Dataset rivers, streams, springs, and waterbodies."),
    USOverlayKind.TOWERS: ("FCC / FAA Obstacle Towers", "Public Domain", "Registered antenna structures and vertical obstructions."),
    USOverlayKind.ENC: ("NOAA ENC Marine Charts", "Public Domain", "Coastal depth contours and navigation aids from NOAA Electronic Navigational Charts."),
    USOverlayKind.US_TRAILS: ("USFS / NPS Trails & Rec", "Public Domain", "Official US Forest Service and National Park Service trail networks and trailheads."),
    USOverlayKind.US_PUBLIC_LANDS: ("USGS PAD-US Public Lands", "Public Domain", "Protected Areas Database boundaries (National Forests, BLM, Parks, Wilderness)."),
}

_AU_OVERLAY_SPECS: dict[AUOverlayKind, tuple[str, str, str]] = {
    AUOverlayKind.AU_HYDRO: ("GA Surface Hydrology", "CC BY 4.0", "Geoscience Australia rivers, creeks, waterholes, and dams."),
    AUOverlayKind.AU_PARKS: ("CAPAD Protected Areas", "CC BY 4.0", "Australian National Parks, Nature Reserves, and State Conservation Areas."),
    AUOverlayKind.AU_INFRA: ("GA Energy & Comms Infrastructure", "CC BY 4.0", "Major transmission lines, substations, and pipelines."),
    AUOverlayKind.AU_AVIATION: ("GA Airfields & Helipads", "CC BY 4.0", "Australian runways, bush airstrips, and landing grounds."),
    AUOverlayKind.AU_STATE_TRAILS: ("Auto State Fire Trails & POIs", "CC BY 4.0", "Automatically selects the official state/territory fire trails, walking tracks, huts, and camp POIs."),
    AUOverlayKind.AU_NSW_TRAILS: ("NSW NPWS & Spatial Trails", "CC BY 4.0", "New South Wales fire trails, walking tracks, and park POIs."),
    AUOverlayKind.AU_VIC_TRAILS: ("VIC DEECA Trails", "CC BY 4.0", "Victoria backcountry tracks, fire access roads, and huts."),
    AUOverlayKind.AU_TAS_TRAILS: ("TAS LIST Tracks & Huts", "CC BY 4.0", "Tasmania bushwalking tracks, huts, and overland routes."),
    AUOverlayKind.AU_QLD_TRAILS: ("QLD Parks Trails", "CC BY 4.0", "Queensland national park trails and camp POIs."),
    AUOverlayKind.AU_WA_TRAILS: ("WA DBCA Trails", "CC BY 4.0", "Western Australia Bibbulmun/Munda Biddi and park trails."),
    AUOverlayKind.AU_SA_TRAILS: ("SA DEW Trails", "CC BY 4.0", "South Australia national park and Heysen trails."),
    AUOverlayKind.AU_NT_TRAILS: ("NT Parks Trails", "CC BY 4.0", "Northern Territory Larapinta and park walking tracks."),
    AUOverlayKind.AU_ACT_TRAILS: ("ACT Parks Trails", "CC BY 4.0", "Australian Capital Territory Namadgi fire trails and tracks."),
}

_NZ_OVERLAY_SPECS: dict[NZOverlayKind, tuple[str, str, str]] = {
    NZOverlayKind.NZ_DOC_TRAILS: (
        "NZ DOC Tracks & Huts",
        "CC BY 4.0",
        "Department of Conservation Great Walks, tramping tracks, backcountry huts, and campsites.",
    ),
}

_CA_OVERLAY_SPECS: dict[CAOverlayKind, tuple[str, str, str]] = {
    CAOverlayKind.CA_CANVEC_TRAILS: (
        "Canada CanVec / Parks Canada Trails",
        "OGL-Canada",
        "Official Canadian backcountry trails, park paths, and recreation POIs.",
    ),
}

_OUTPUT_FORMAT_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("pdf", "True-Scale Print PDF", ".pdf", "Dual-stack crisp vector + hillshade PDF in native local UTM with 1 km MGRS collar & WMM2025 declination diagram."),
    ("geopdf", "Georeferenced GeoPDF", ".pdf", "ISO 32000 georeferenced PDF compatible with Avenza Maps and GIS viewers."),
    ("mbtiles", "ATAK / Offline MBTiles", ".mbtiles", "Raster tile pyramid archive for ATAK, CalTopo, and mobile map apps."),
    ("kmz", "Garmin Custom Maps KMZ", ".kmz", "Tiled JPEG ground-overlay archive respecting Garmin handheld tile limits."),
    ("gpkg_tiles", "OGC GeoPackage Tiles", ".gpkg", "Standards-compliant GeoPackage raster tiles for QGIS, ATAK, and field GIS."),
    ("osmand_sqlitedb", "OsmAnd SQLiteDB", ".sqlitedb", "Offline tile database ready to drop directly into OsmAnd."),
    ("field_bundle_zip", "Field Bundle ZIP", ".zip", "Combined archive bundling all generated print and device exports with manifest metadata."),
)

_RF_BAND_PRESETS: tuple[RFBandPreset, ...] = (
    RFBandPreset(
        id="lora-915",
        label="LoRa / Meshtastic (915 MHz)",
        freq_mhz=915.0,
        region="US / CA / AU / NZ",
        description="915 MHz ISM band for Meshtastic, LoRaWAN, and off-grid telemetry.",
    ),
    RFBandPreset(
        id="lora-868",
        label="LoRa / Meshtastic (868 MHz)",
        freq_mhz=868.0,
        region="EU / UK",
        description="868 MHz European ISM band for LoRa and Meshtastic nodes.",
    ),
    RFBandPreset(
        id="uhf-cb-477",
        label="UHF CB (477 MHz)",
        freq_mhz=477.0,
        region="AU / NZ",
        description="477 MHz 80-channel UHF CB handheld and repeater communications.",
    ),
    RFBandPreset(
        id="gmrs-462",
        label="GMRS / FRS (462.625 MHz)",
        freq_mhz=462.625,
        region="US / CA",
        description="462 MHz GMRS simplex and repeater communications.",
    ),
    RFBandPreset(
        id="vhf-146",
        label="VHF 2m / SAR (146.52 MHz)",
        freq_mhz=146.52,
        region="Global",
        description="146 MHz VHF 2-meter simplex, backcountry, and search-and-rescue band.",
    ),
    RFBandPreset(
        id="ism-2400",
        label="2.4 GHz ISM / Mesh",
        freq_mhz=2437.0,
        region="Global",
        description="2.4 GHz Wi-Fi, MANET, and short-range directional data links.",
    ),
    RFBandPreset(
        id="ism-5800",
        label="5.8 GHz Point-to-Point",
        freq_mhz=5800.0,
        region="Global",
        description="5.8 GHz high-throughput line-of-sight directional backhaul and FPV video.",
    ),
)

_TACTICAL_GRAPHIC_SPECS: tuple[TacticalGraphicCapability, ...] = (
    TacticalGraphicCapability(
        id="range_ring",
        name="Range Rings",
        description="Concentric distance circles around a center point (radii in km).",
        required_fields=["points (1 center [lat, lon])", "radii_km"],
    ),
    TacticalGraphicCapability(
        id="bearing_line",
        name="Bearing Line",
        description="Directional azimuth line connecting two or more coordinates.",
        required_fields=["points (2+ [lat, lon] vertices)"],
    ),
    TacticalGraphicCapability(
        id="corridor",
        name="Route Corridor",
        description="Multi-segment corridor path marking an approach or ingress route.",
        required_fields=["points (2+ [lat, lon] vertices)"],
    ),
    TacticalGraphicCapability(
        id="sector",
        name="Sector / AOI Polygon",
        description="Closed polygon area of interest or search sector.",
        required_fields=["points (3+ [lat, lon] vertices)"],
    ),
)


def build_capabilities_response(
    *,
    auth_mode: Literal["none", "hosted"],
    caller_role: AuthRole,
    authenticated: bool,
    caller_capabilities: AuthCapabilities,
    installed_packs_count: int = 0,
    brand_name: str = "GridForge",
    public_url: str | None = None,
    signed_url_ttl_seconds: int = 3600,
) -> CapabilitiesResponse:
    """Build the complete ``GET /api/capabilities`` discovery payload."""
    paper_sizes = [
        PaperSizeCapability(
            id=ps.value,
            label=_PAPER_LABELS[ps],
            width_mm=PAPER_DIMENSIONS_MM[ps][0],
            height_mm=PAPER_DIMENSIONS_MM[ps][1],
        )
        for ps in PaperSize
    ]
    color_modes = [
        ColorModeCapability(
            id=cm.value,
            label=_COLOR_MODE_SPECS[cm][0],
            description=_COLOR_MODE_SPECS[cm][1],
        )
        for cm in ColorMode
    ]
    dem_sources = [
        DemSourceCapability(id=ds.value, **_DEM_SOURCE_SPECS[ds])
        for ds in DemSourceKind
    ]
    regional_overlays: dict[str, list[OverlayCapability]] = {
        "us": [
            OverlayCapability(
                id=ov.value,
                name=_US_OVERLAY_SPECS[ov][0],
                license=_US_OVERLAY_SPECS[ov][1],
                description=_US_OVERLAY_SPECS[ov][2],
            )
            for ov in USOverlayKind
        ],
        "au": [
            OverlayCapability(
                id=ov.value,
                name=_AU_OVERLAY_SPECS[ov][0],
                license=_AU_OVERLAY_SPECS[ov][1],
                description=_AU_OVERLAY_SPECS[ov][2],
            )
            for ov in AUOverlayKind
        ],
        "nz": [
            OverlayCapability(
                id=ov.value,
                name=_NZ_OVERLAY_SPECS[ov][0],
                license=_NZ_OVERLAY_SPECS[ov][1],
                description=_NZ_OVERLAY_SPECS[ov][2],
            )
            for ov in NZOverlayKind
        ],
        "ca": [
            OverlayCapability(
                id=ov.value,
                name=_CA_OVERLAY_SPECS[ov][0],
                license=_CA_OVERLAY_SPECS[ov][1],
                description=_CA_OVERLAY_SPECS[ov][2],
            )
            for ov in CAOverlayKind
        ],
    }

    allowed_outputs = set(caller_capabilities.allowed_outputs)
    output_formats = [
        OutputFormatCapability(
            id=fmt_id,
            name=name,
            extension=ext,
            description=desc,
            allowed_for_caller=(fmt_id in allowed_outputs),
        )
        for fmt_id, name, ext, desc in _OUTPUT_FORMAT_SPECS
    ]

    return CapabilitiesResponse(
        service="gridforge",
        brand_name=brand_name,
        version=__version__,
        auth_mode=auth_mode,
        caller_role=caller_role,
        authenticated=authenticated,
        public_url=public_url,
        mcp_endpoint="/mcp",
        openapi_url="/openapi.json",
        llms_txt_url="/llms.txt",
        endpoints=CapabilitiesEndpoints(),
        scales=list(VALID_SCALES),
        paper_sizes=paper_sizes,
        orientations=[o.value for o in Orientation],
        color_modes=color_modes,
        units=[u.value for u in Units],
        dem_sources=dem_sources,
        regional_overlays=regional_overlays,
        output_formats=output_formats,
        rf_band_presets=list(_RF_BAND_PRESETS),
        tactical_graphics=list(_TACTICAL_GRAPHIC_SPECS),
        waypoint_categories=[c.value for c in WaypointCategory],
        installed_packs_count=installed_packs_count,
        signed_url_ttl_seconds=signed_url_ttl_seconds,
        caller_capabilities=caller_capabilities,
        notes={
            "projection": "Native local UTM (EPSG:326xx/327xx) only; never Web Mercator for print.",
            "synchronous_render": "Pass wait=true&timeout_s=120 to POST /api/jobs to block until terminal status (200 OK).",
            "signed_urls": "Completed job artifacts include signed_download_url (?sig=...) and /api/jobs/{job_id}/pages returns signed_preview_url PNG links.",
        },
    )
