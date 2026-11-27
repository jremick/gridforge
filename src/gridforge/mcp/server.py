"""Native GridForge MCP server with Q&A tools, Pillow PNG visual cards, and SEP-1865 MCP Apps.

Strictly zero ``qgis`` / ``PyQt5`` imports. Targets either an in-process FastAPI ASGI app
via ``httpx.ASGITransport`` or a remote GridForge/GridMaps HTTP base URL.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from PIL import Image

from gridforge import __version__
from gridforge.config import (
    AUOverlayKind,
    CAOverlayKind,
    ColorMode,
    DemSourceKind,
    NZOverlayKind,
    Orientation,
    PaperSize,
    Units,
    USOverlayKind,
    VALID_SCALES,
)
from gridforge.geo.crs import parse_center
from gridforge.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    MCP_APP_MIME_TYPE,
    METHOD_NOT_FOUND,
    MCPError,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPResourceDefinition,
    MCPToolDefinition,
    build_initialize_result,
    call_tool_result,
    image_content,
    jsonrpc_error,
    jsonrpc_result,
    resource_link_content,
    resource_read_result,
    text_content,
)
from gridforge.mcp.visuals import (
    render_footprint_card_png,
    render_rf_link_chart_png,
    render_route_card_png,
)

logger = logging.getLogger(__name__)

# SEP-1865 MCP Apps resource & tool metadata constants (shared contract with web/mcp-app)
APP_URI: str = "ui://gridforge/app.html"
APP_MIME_TYPE: str = MCP_APP_MIME_TYPE
APP_RESOURCE_META: dict[str, Any] = {"ui": {"prefersBorder": False}}

CAPABILITIES_URI: str = "gridforge://capabilities"
GUIDE_URI: str = "gridforge://guide"

INLINE_UI: dict[str, Any] = {
    "ui": {"resourceUri": APP_URI},
    "ui/resourceUri": APP_URI,
}
STUDIO_UI: dict[str, Any] = {
    "ui": {"resourceUri": APP_URI},
    "ui/resourceUri": APP_URI,
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "GridForge Studio"},
            {"type": "global", "title": "GridForge Maps"},
        ]
    },
}
APP_ONLY: dict[str, Any] = {
    "ui": {"visibility": ["app"]},
}

_SERVER_NAME: str = "gridforge"
_SERVER_INSTRUCTIONS: str = (
    "GridForge generates physically true-scale local UTM MGRS topographic PDFs, "
    "ATAK MBTiles, Garmin KMZ, GeoPackage, OsmAnd maps, curved-Earth RF/Fresnel link analyses, "
    "DEM viewsheds, WMM2025 declination briefings, and route strip atlases. "
    "Use answer_location_briefing for place/MGRS/declination questions, analyze_rf_link for radio LOS, "
    "compute_viewshed for visibility coverage, measure_route or plan_strip_atlas for routes, "
    "and render_map to generate printable PDFs or offline device archives."
)

_MAX_TRAJECTORY_PNG_BYTES: int = 700_000

# Curated global studio presets for open_gridforge_studio
_STUDIO_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "blue-mountains",
        "name": "Blue Mountains (Katoomba)",
        "region": "NSW, Australia · 56H",
        "center": "-33.732,150.312",
        "scale": 25000,
        "paper": "a4",
        "color_mode": "color",
    },
    {
        "id": "mt-kosciuszko",
        "name": "Mt Kosciuszko Main Range",
        "region": "NSW, Australia · 55H",
        "center": "-36.456,148.263",
        "scale": 25000,
        "paper": "a4",
        "color_mode": "alpine",
    },
    {
        "id": "cradle-mountain",
        "name": "Cradle Mountain / Overland",
        "region": "TAS, Australia · 55G",
        "center": "-41.684,145.951",
        "scale": 25000,
        "paper": "a4",
        "color_mode": "color",
    },
    {
        "id": "tongariro",
        "name": "Tongariro Alpine Crossing",
        "region": "North Island, NZ · 60H",
        "center": "-39.133,175.642",
        "scale": 25000,
        "paper": "a4",
        "color_mode": "alpine",
    },
    {
        "id": "yosemite",
        "name": "Yosemite Valley",
        "region": "California, US · 11S",
        "center": "37.7456,-119.5936",
        "scale": 24000,
        "paper": "letter",
        "color_mode": "alpine",
    },
    {
        "id": "rocky-mtn",
        "name": "Rocky Mountain NP (Longs Peak)",
        "region": "Colorado, US · 13T",
        "center": "40.2549,-105.6151",
        "scale": 24000,
        "paper": "letter",
        "color_mode": "color",
    },
    {
        "id": "banff",
        "name": "Banff & Lake Louise",
        "region": "Alberta, Canada · 11U",
        "center": "51.4167,-116.2167",
        "scale": 25000,
        "paper": "letter",
        "color_mode": "alpine",
    },
    {
        "id": "chamonix",
        "name": "Chamonix / Mont Blanc",
        "region": "Alps, France · 32T",
        "center": "45.9237,6.8694",
        "scale": 25000,
        "paper": "a4",
        "color_mode": "alpine",
    },
)

_FALLBACK_APP_HTML: str = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>GridForge MCP App</title>
<style>
  body { margin: 0; font-family: system-ui, sans-serif; background: #0b1117; color: #e8eef5; padding: 16px; }
  .card { border: 1px solid #263547; background: #111a24; border-radius: 10px; padding: 14px; }
  h1 { margin: 0 0 8px; font-size: 16px; color: #f59e0b; }
  p { margin: 0; font-size: 13px; color: #8fa3b8; }
</style>
</head>
<body>
  <div class="card">
    <h1>GridForge Tactical Topographic Studio</h1>
    <p>Interactive MCP App bundle placeholder. Build <code>web/mcp-app</code> with <code>npm --prefix web run build:mcp-app</code> to enable live interactive controls.</p>
  </div>
</body>
</html>
"""

_GUIDE_MARKDOWN: str = """# GridForge AI & Tactical Cartography Guide

GridForge generates physically **true-scale local UTM** MGRS topographic maps (`<= 0.15 mm` scale error,
`< 0.05 mm` skew — never Web Mercator for print sheets), with WMM2025 magnetic declination, dual-stack
vector crispness, curved-Earth RF Fresnel analysis, DEM viewsheds, and offline field exports.

## Recommended Regional Open Data (Zero-Auth, Commercial-Friendly)
- **United States**: `dem_source="usgs3dep"` (1m/10m 3DEP, NAVD88), `us_overlays=["us-trails", "us-public-lands", "nhd"]` (`fema`, `towers`, `enc` also available)
- **Canada**: `dem_source="ca-hrdem"` (NRCan HRDEM 1m–2m, CGVD2013), `ca_overlays=["ca-canvec-trails"]`
- **Australia**: `dem_source="au-elvis"` (Geoscience Australia / ELVIS, AHD), `au_overlays=["au-state-trails", "au-hydro", "au-parks"]` (`au-state-trails` auto-selects across all 8 states/territories)
- **New Zealand**: `dem_source="nz-linz"` (LINZ 8m/1m, NZVD2016), `nz_overlays=["nz-doc-trails"]`
- **Global**: `dem_source="auto"` or `"glo30"` (Copernicus GLO-30 30m, EGM2008 + OpenStreetMap)

## 4 Dedicated Cartography Themes
- `color`: Classic daylight military/backcountry topographic palette
- `bw`: High-contrast monochrome laser-printer theme
- `night-red`: Red-light / NVG-friendly low-glare dark tactical theme
- `alpine`: High-relief glacier/scree/ridge theme (pairs well with `carto.show_slope_shading=true`)

## Typical Tool Workflows
1. **Location Q&A**: Call `answer_location_briefing` with a place name, `"lat,lon"`, or MGRS string.
2. **RF / LoRa LOS**: Call `analyze_rf_link` with TX/RX coordinates and `freq_mhz` (915, 868, 477, 462, 146, 2400 MHz).
3. **Viewshed Coverage**: Call `compute_viewshed` for terrain-masked visibility area and coverage GeoJSON.
4. **Routes & Strip Atlases**: Call `measure_route` for leg-by-leg True/Grid/Mag azimuths & NATO mils, or `plan_strip_atlas` for multi-sheet corridor fitting.
5. **Printable Maps & Device Archives**: Call `render_map` (PDF, GeoPDF, ATAK MBTiles, Garmin KMZ, GeoPackage, OsmAnd SQLiteDB, or Field Bundle ZIP), then inspect secondary pages via `get_job_page_preview`.
"""


def _format_ew(angle_deg: float) -> str:
    return f"{abs(angle_deg):.2f}° {'E' if angle_deg >= 0 else 'W'}"


def _recommend_regional_data(
    lat: float,
    lon: float,
    installed_packs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Recommend open-data DEM, regional overlays, and matching offline packs for ``(lat, lon)``."""
    matching_packs: list[str] = []
    for pack in installed_packs:
        bbox = pack.get("bbox_wgs84")
        if isinstance(bbox, dict):
            try:
                if (
                    float(bbox["min_x"]) <= lon <= float(bbox["max_x"])
                    and float(bbox["min_y"]) <= lat <= float(bbox["max_y"])
                ):
                    pid = str(pack.get("pack_id") or "").strip()
                    if pid:
                        matching_packs.append(pid)
            except (KeyError, TypeError, ValueError):
                continue

    # Australia
    if 112.0 <= lon <= 155.0 and -45.0 <= lat <= -9.5:
        return {
            "dem_source": "au-elvis",
            "region_label": "Australia (ELVIS / Geoscience Australia + 8-State Trails)",
            "overlays": ["au-state-trails", "au-hydro", "au-parks"],
            "matching_packs": matching_packs,
            "overlay_param": "au_overlays",
        }
    # New Zealand
    if 165.0 <= lon <= 179.5 and -48.0 <= lat <= -33.5:
        return {
            "dem_source": "nz-linz",
            "region_label": "New Zealand (LINZ 8m/1m DEM + DOC Tracks & Huts)",
            "overlays": ["nz-doc-trails"],
            "matching_packs": matching_packs,
            "overlay_param": "nz_overlays",
        }
    # Canada (north of 49N, plus southern Ontario/Quebec/Maritimes pockets)
    is_canada = (
        (-141.0 <= lon <= -52.0 and 49.0 <= lat <= 83.5)
        or (-83.5 <= lon <= -74.0 and 42.0 <= lat < 49.0 and lat >= 43.0)
        or (-74.0 <= lon <= -59.0 and 45.0 <= lat < 49.0)
    )
    # United States (CONUS, Alaska, Hawaii)
    is_us_conus = -125.5 <= lon <= -66.5 and 24.3 <= lat <= 49.0
    is_us_ak = -170.0 <= lon <= -129.9 and 51.0 <= lat <= 72.0
    is_us_hi = -161.0 <= lon <= -154.5 and 18.5 <= lat <= 22.5

    if is_us_ak or is_us_hi or (is_us_conus and not is_canada):
        return {
            "dem_source": "usgs3dep",
            "region_label": "United States (USGS 3DEP + USFS Trails & PAD-US Public Lands)",
            "overlays": ["us-trails", "us-public-lands", "nhd"],
            "matching_packs": matching_packs,
            "overlay_param": "us_overlays",
        }
    if is_canada:
        return {
            "dem_source": "ca-hrdem",
            "region_label": "Canada (NRCan HRDEM + CanVec / Parks Canada Trails)",
            "overlays": ["ca-canvec-trails"],
            "matching_packs": matching_packs,
            "overlay_param": "ca_overlays",
        }
    return {
        "dem_source": "glo30",
        "region_label": "Global (Copernicus GLO-30 30m DEM + OpenStreetMap)",
        "overlays": [],
        "matching_packs": matching_packs,
        "overlay_param": None,
    }


def _escape_delimited(raw: str) -> str:
    return (
        str(raw or "")
        .replace("%", "%25")
        .replace(",", "%2C")
        .replace(";", "%3B")
    )


def build_studio_url(
    public_url: str | None,
    *,
    center: str | None = None,
    scale: int | None = None,
    paper: str | None = None,
    orientation: str | None = None,
    color_mode: str | None = None,
    units: str | None = None,
    dpi: int | None = None,
    dem_source: str | None = None,
    pack_id: str | None = None,
    grid_cols: int = 1,
    grid_rows: int = 1,
    title: str | None = None,
    au_overlays: list[str] | None = None,
    nz_overlays: list[str] | None = None,
    ca_overlays: list[str] | None = None,
    us_overlays: list[str] | None = None,
    waypoints: list[dict[str, Any]] | None = None,
    viewsheds: list[dict[str, Any]] | None = None,
    rf_links: list[dict[str, Any]] | None = None,
    include_calibration_page: bool = False,
    include_route_card: bool = False,
    include_elevation_profile: bool = False,
    include_atlas_index: bool = False,
    include_rf_link_card: bool = False,
    show_fold_marks: bool = False,
) -> str | None:
    """Build a Studio deep-link URL matching ``web/src/deeplink.ts`` query parameters."""
    if not public_url:
        return None
    base = public_url.rstrip("/")
    params: list[tuple[str, str]] = []
    if center:
        params.append(("center", center.replace(" ", "")))
    if scale is not None:
        params.append(("scale", str(scale)))
    if paper:
        params.append(("paper", str(paper).lower()))
    if orientation:
        params.append(("orientation", str(orientation).lower()))
    if color_mode:
        params.append(("mode", str(color_mode).lower()))
    if units:
        params.append(("units", str(units).lower()))
    if dpi is not None and dpi != 300:
        params.append(("dpi", str(dpi)))
    if dem_source and str(dem_source).lower() != "auto":
        params.append(("dem", str(dem_source).lower()))
    if pack_id:
        params.append(("pack", str(pack_id)))
    if grid_cols * grid_rows > 1:
        params.append(("grid", f"{grid_cols}x{grid_rows}"))
    if title and title.strip():
        params.append(("title", title.strip()[:80]))
    if au_overlays:
        params.append(("au", ",".join(au_overlays)))
    if nz_overlays:
        params.append(("nz", ",".join(nz_overlays)))
    if ca_overlays:
        params.append(("ca", ",".join(ca_overlays)))
    if us_overlays:
        params.append(("us", ",".join(us_overlays)))
    if viewsheds:
        los_items = []
        for vs in viewsheds[:4]:
            lat = vs.get("lat")
            lon = vs.get("lon")
            if lat is None or lon is None:
                continue
            obs_h = float(vs.get("observer_height_m", 2.0))
            tgt_h = float(vs.get("target_height_m", 1.5))
            max_km = float(vs.get("max_distance_m", 10000.0)) / 1000.0
            lbl = _escape_delimited(str(vs.get("label") or "OBS")[:24])
            los_items.append(f"{float(lat):.6f},{float(lon):.6f},{obs_h:g},{tgt_h:g},{max_km:g},{lbl}")
        if los_items:
            params.append(("los", ";".join(los_items)))
    if waypoints:
        wp_items = []
        for idx, wp in enumerate(waypoints[:50], start=1):
            lat = wp.get("lat")
            lon = wp.get("lon")
            if lat is None or lon is None:
                continue
            cat = str(wp.get("category") or "waypoint").lower()
            lbl = _escape_delimited(str(wp.get("label") or f"WP{idx}")[:40])
            wp_items.append(f"{float(lat):.6f},{float(lon):.6f},{cat},{lbl}")
        if wp_items:
            params.append(("wp", ";".join(wp_items)))
    if rf_links:
        rf_items = []
        for idx, link in enumerate(rf_links[:10], start=1):
            try:
                fl = float(link["from_lat"])
                flo = float(link["from_lon"])
                tl = float(link["to_lat"])
                tlo = float(link["to_lon"])
            except (KeyError, TypeError, ValueError):
                continue
            fh = float(link.get("from_height_m", 2.0))
            th = float(link.get("to_height_m", 2.0))
            freq = float(link.get("freq_mhz", 915.0))
            kf = float(link.get("k_factor", 1.333))
            flbl = _escape_delimited(str(link.get("from_label") or f"A{idx}")[:24])
            tlbl = _escape_delimited(str(link.get("to_label") or f"B{idx}")[:24])
            rf_items.append(f"{fl:.6f},{flo:.6f},{tl:.6f},{tlo:.6f},{fh:g},{th:g},{freq:g},{kf:.3f},{flbl},{tlbl}")
        if rf_items:
            params.append(("rf", ";".join(rf_items)))
    if include_calibration_page:
        params.append(("cal", "1"))
    if include_route_card:
        params.append(("rc", "1"))
    if include_elevation_profile:
        params.append(("ep", "1"))
    if include_atlas_index:
        params.append(("index", "1"))
    if include_rf_link_card:
        params.append(("rfcard", "1"))
    if show_fold_marks:
        params.append(("fold", "1"))

    if not params:
        return f"{base}/"
    return f"{base}/?{urlencode(params, safe=',;:')}"


def _absolutize_url(path_or_url: str | None, public_url: str | None) -> str | None:
    if not path_or_url:
        return None
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    if public_url and path_or_url.startswith("/"):
        return f"{public_url.rstrip('/')}{path_or_url}"
    return path_or_url


def _fit_png_under_budget(png_bytes: bytes, max_bytes: int = _MAX_TRAJECTORY_PNG_BYTES) -> bytes:
    """Ensure a PNG preview image stays safely below the MCP trajectory size cap."""
    if len(png_bytes) <= max_bytes:
        return png_bytes
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            cur = im.convert("RGB")
            for scale in (0.85, 0.7, 0.55, 0.4):
                nw = max(240, int(cur.width * scale))
                nh = max(240, int(cur.height * scale))
                resized = cur.resize((nw, nh), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                resized.save(buf, format="PNG", optimize=True, compress_level=9)
                out = buf.getvalue()
                if len(out) <= max_bytes:
                    return out
            # Final compact quantized fallback
            quant = cur.resize((max(240, cur.width // 2), max(240, cur.height // 2)), Image.Resampling.LANCZOS)
            quant = quant.quantize(colors=128)
            buf = io.BytesIO()
            quant.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception:
        return png_bytes


def _static_capabilities_fallback(packs_count: int = 0) -> dict[str, Any]:
    """Build a static capabilities summary if the target backend lacks ``GET /api/capabilities``."""
    return {
        "version": __version__,
        "auth_mode": "none",
        "scales": list(VALID_SCALES),
        "paper_sizes": [p.value for p in PaperSize],
        "orientations": [o.value for o in Orientation],
        "color_modes": [
            {"id": ColorMode.COLOR.value, "description": "Classic daylight military/backcountry topographic palette."},
            {"id": ColorMode.BW.value, "description": "High-contrast monochrome theme optimized for laser printers."},
            {"id": ColorMode.NIGHT_RED.value, "description": "Red-light / NVG-friendly low-glare dark tactical palette."},
            {"id": ColorMode.ALPINE.value, "description": "High-relief alpine/glacier theme with vivid contours."},
        ],
        "units": [u.value for u in Units],
        "dem_sources": [
            {"id": DemSourceKind.AUTO.value, "region": "Global (auto regional best)", "vertical_datum": "Auto"},
            {"id": DemSourceKind.USGS3DEP.value, "region": "United States (1m/10m)", "vertical_datum": "NAVD88"},
            {"id": DemSourceKind.CA_HRDEM.value, "region": "Canada (NRCan 1m–2m)", "vertical_datum": "CGVD2013"},
            {"id": DemSourceKind.AU_ELVIS.value, "region": "Australia (ELVIS / GA)", "vertical_datum": "AHD"},
            {"id": DemSourceKind.NZ_LINZ.value, "region": "New Zealand (LINZ 8m/1m)", "vertical_datum": "NZVD2016"},
            {"id": DemSourceKind.GLO30.value, "region": "Global (Copernicus 30m)", "vertical_datum": "EGM2008"},
        ],
        "regional_overlays": {
            "us": [o.value for o in USOverlayKind],
            "au": [o.value for o in AUOverlayKind],
            "nz": [o.value for o in NZOverlayKind],
            "ca": [o.value for o in CAOverlayKind],
        },
        "outputs": [
            "pdf",
            "geopdf",
            "mbtiles",
            "kmz",
            "gpkg_tiles",
            "osmand_sqlitedb",
            "field_bundle_zip",
        ],
        "rf_band_presets": [
            {"label": "LoRa 915 MHz (US/AU/NZ)", "freq_mhz": 915.0},
            {"label": "LoRa 868 MHz (EU)", "freq_mhz": 868.0},
            {"label": "UHF CB 477 MHz (AU/NZ)", "freq_mhz": 477.0},
            {"label": "GMRS / FRS 462 MHz (US)", "freq_mhz": 462.0},
            {"label": "VHF 146 MHz (2m Amateur)", "freq_mhz": 146.0},
            {"label": "2.4 GHz ISM / Wi-Fi", "freq_mhz": 2400.0},
            {"label": "5.8 GHz Point-to-Point", "freq_mhz": 5800.0},
        ],
        "tactical_graphics": ["bearing_line", "range_ring", "corridor", "sector"],
        "installed_packs_count": packs_count,
        "mcp_endpoint": "/mcp",
        "openapi_url": "/openapi.json",
    }


def _extract_error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            detail = body["detail"]
            return detail if isinstance(detail, str) else json.dumps(detail)
    except Exception:
        pass
    text = (resp.text or "").strip()
    return text[:400] if text else f"HTTP {resp.status_code}"


class GridForgeMCPServer:
    """Native GridForge MCP server supporting JSON-RPC 2.0, PNG visual cards, and SEP-1865 MCP Apps."""

    def __init__(
        self,
        *,
        app: Any | None = None,
        get_app: Callable[[], Any] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        public_url: str | None = None,
        app_html_path: Path | str | None = None,
        http_timeout_s: float = 150.0,
    ) -> None:
        if app is None and get_app is None and not base_url:
            raise ValueError("Provide either 'app', 'get_app', or remote 'base_url'.")
        self._app = app
        self._get_app = get_app
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = (api_key or "").strip() or None
        self._public_url = (public_url or "").strip().rstrip("/") or None
        self._app_html_path = Path(app_html_path) if app_html_path is not None else None
        self._http_timeout_s = float(http_timeout_s)

        self._tools: tuple[MCPToolDefinition, ...] = self._build_tool_definitions()
        self._tool_map: dict[str, MCPToolDefinition] = {t.name: t for t in self._tools}
        self._resources: tuple[MCPResourceDefinition, ...] = self._build_resource_definitions()
        self._prompts: tuple[MCPPromptDefinition, ...] = self._build_prompt_definitions()

    # ------------------------------------------------------------------
    # Tool / Resource / Prompt Catalog Definitions
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_definitions() -> tuple[MCPToolDefinition, ...]:
        scale_enum = list(VALID_SCALES)
        paper_enum = [p.value for p in PaperSize]
        orient_enum = [o.value for o in Orientation]
        mode_enum = [m.value for m in ColorMode]
        units_enum = [u.value for u in Units]
        dem_enum = [d.value for d in DemSourceKind]

        return (
            MCPToolDefinition(
                name="open_gridforge_studio",
                title="Open GridForge Studio",
                description=(
                    "Open the interactive GridForge Studio map explorer & quick-build launcher "
                    "(all arguments optional; call with {} to open the interactive studio panel)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "Optional starting place name, 'lat,lon', or MGRS."},
                        "scale": {"type": "integer", "enum": scale_enum, "description": "Optional starting map scale."},
                        "paper": {"type": "string", "enum": paper_enum, "description": "Optional paper size."},
                        "color_mode": {"type": "string", "enum": mode_enum, "description": "Optional cartography theme."},
                    },
                    "required": [],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=STUDIO_UI,
            ),
            MCPToolDefinition(
                name="answer_location_briefing",
                title="Location Navigation & Map Briefing",
                description=(
                    "Answer navigation, MGRS, UTM zone, WMM2025 magnetic declination, grid convergence, "
                    "G-M angle, sheet footprint, and regional open-data questions for any place name, "
                    "'lat,lon', or MGRS coordinate. Returns a natural-language briefing, crisp footprint "
                    "PNG schematic, and interactive map explorer."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "Place name (e.g. 'Mt Kosciuszko'), 'lat,lon', or MGRS string."},
                        "question": {"type": "string", "description": "Optional specific user question to answer directly."},
                        "scale": {"type": "integer", "enum": scale_enum, "default": 25000},
                        "paper": {"type": "string", "enum": paper_enum, "default": "a4"},
                        "orientation": {"type": "string", "enum": orient_enum, "default": "portrait"},
                        "color_mode": {"type": "string", "enum": mode_enum, "default": "color"},
                        "units": {"type": "string", "enum": units_enum, "default": "metric"},
                        "grid_cols": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                        "grid_rows": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                        "date_iso": {"type": "string", "description": "Optional YYYY-MM-DD date for WMM2025 declination."},
                        "include_image": {"type": "boolean", "default": True},
                    },
                    "required": ["location"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="geocode_place",
                title="Geocode Place Name",
                description="Search OpenStreetMap Nominatim for place names and return WGS84 coordinates inside UTM coverage.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Place or landmark search query (min 2 characters)."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    "required": ["query"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="preview_footprint",
                title="Preview True-Scale UTM Sheet Footprint",
                description=(
                    "Compute true-scale local UTM footprint polygons (single sheet or 1x1..3x3 atlas), "
                    "1 km MGRS preview grid lines, and WMM2025 magnetic declination with a crisp PNG schematic."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "string", "description": "Center as 'lat,lon', MGRS string, or place name."},
                        "scale": {"type": "integer", "enum": scale_enum, "default": 25000},
                        "paper": {"type": "string", "enum": paper_enum, "default": "a4"},
                        "orientation": {"type": "string", "enum": orient_enum, "default": "portrait"},
                        "color_mode": {"type": "string", "enum": mode_enum, "default": "color"},
                        "units": {"type": "string", "enum": units_enum, "default": "metric"},
                        "date_iso": {"type": "string"},
                        "grid_cols": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                        "grid_rows": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                        "title": {"type": "string"},
                        "waypoints": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "lat": {"type": "number"},
                                    "lon": {"type": "number"},
                                    "category": {"type": "string", "enum": ["waypoint", "camp", "water", "hazard", "lz"]},
                                },
                                "required": ["label", "lat", "lon"],
                            },
                        },
                        "include_image": {"type": "boolean", "default": True},
                    },
                    "required": ["center"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="analyze_rf_link",
                title="Analyze Curved-Earth RF / Fresnel Link",
                description=(
                    "Compute a point-to-point curved-Earth (k=4/3) RF terrain profile, 1st Fresnel zone clearance, "
                    "free-space path loss (FSPL dB), CLEAR/MARGINAL/OBSTRUCTED verdict, True/Grid/Mag bearings "
                    "& NATO mils, plus a crisp PNG Fresnel chart and interactive hover-scrub chart."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "from_lat": {"type": "number", "minimum": -80, "maximum": 84},
                        "from_lon": {"type": "number", "minimum": -180, "maximum": 180},
                        "to_lat": {"type": "number", "minimum": -80, "maximum": 84},
                        "to_lon": {"type": "number", "minimum": -180, "maximum": 180},
                        "freq_mhz": {"type": "number", "default": 915.0, "description": "Carrier frequency in MHz (e.g. 915, 868, 477, 462, 146, 2400)."},
                        "from_height_m": {"type": "number", "default": 2.0, "minimum": 0, "maximum": 500},
                        "to_height_m": {"type": "number", "default": 2.0, "minimum": 0, "maximum": 500},
                        "k_factor": {"type": "number", "default": 1.3333333333333333},
                        "from_label": {"type": "string", "default": "TX"},
                        "to_label": {"type": "string", "default": "RX"},
                        "dem_source": {"type": "string", "enum": dem_enum, "default": "auto"},
                        "pack_id": {"type": "string"},
                        "date_iso": {"type": "string"},
                        "include_image": {"type": "boolean", "default": True},
                    },
                    "required": ["from_lat", "from_lon", "to_lat", "to_lon"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="compute_viewshed",
                title="Compute Terrain-Masked RF / LOS Viewshed",
                description=(
                    "Compute a DEM terrain-masked RF / Line-of-Sight viewshed from an observer location, "
                    "returning visible area (km²), coverage GeoJSON, range ring, and a tactical PNG schematic."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "lat": {"type": "number", "minimum": -80, "maximum": 84},
                        "lon": {"type": "number", "minimum": -180, "maximum": 180},
                        "label": {"type": "string", "default": "Observer"},
                        "observer_height_m": {"type": "number", "default": 2.0, "minimum": 0, "maximum": 500},
                        "target_height_m": {"type": "number", "default": 1.5, "minimum": 0, "maximum": 500},
                        "max_distance_m": {"type": "number", "default": 10000.0, "minimum": 250, "maximum": 25000},
                        "dem_source": {"type": "string", "enum": dem_enum, "default": "auto"},
                        "pack_id": {"type": "string"},
                        "include_image": {"type": "boolean", "default": True},
                    },
                    "required": ["lat", "lon"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="measure_route",
                title="Measure Tactical Route or Ruler Legs",
                description=(
                    "Compute geodesic leg distances, True/Grid/WMM2025 Magnetic bearings, NATO mils, "
                    "10m MGRS waypoints, and optional DEM elevation grades for waypoints or ruler points, "
                    "with a tactical Route Card PNG schematic."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "waypoints": {
                            "type": "array",
                            "description": "Named tactical waypoints for /api/route-metrics.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "lat": {"type": "number"},
                                    "lon": {"type": "number"},
                                    "category": {"type": "string", "enum": ["waypoint", "camp", "water", "hazard", "lz"]},
                                    "notes": {"type": "string"},
                                },
                                "required": ["label", "lat", "lon"],
                            },
                        },
                        "points": {
                            "type": "array",
                            "description": "Ordered [{lat, lon}] or [[lat, lon]] coordinates for /api/ruler-metrics.",
                            "items": {"type": ["object", "array"]},
                        },
                        "units": {"type": "string", "enum": units_enum, "default": "metric"},
                        "date_iso": {"type": "string"},
                        "dem_source": {"type": "string", "enum": dem_enum},
                        "pack_id": {"type": "string"},
                        "title": {"type": "string"},
                        "include_image": {"type": "boolean", "default": True},
                    },
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="plan_strip_atlas",
                title="Plan Route Strip Atlas Sheets",
                description=(
                    "Auto-fit a minimum-count sequence of overlapping true-scale local UTM sheets (S1..Sn) "
                    "covering 100% of a GeoJSON route LineString/FeatureCollection."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "route_geojson": {"type": "object", "description": "GeoJSON LineString, Feature, or FeatureCollection."},
                        "scale": {"type": "integer", "enum": scale_enum, "default": 25000},
                        "paper": {"type": "string", "enum": paper_enum, "default": "a4"},
                        "orientation": {"type": "string", "enum": orient_enum, "default": "portrait"},
                        "units": {"type": "string", "enum": units_enum, "default": "metric"},
                        "overlap_pct": {"type": "number", "minimum": 0, "maximum": 40, "default": 10.0},
                        "max_sheets": {"type": "integer", "minimum": 1, "maximum": 24, "default": 12},
                        "title": {"type": "string"},
                        "include_image": {"type": "boolean", "default": True},
                    },
                    "required": ["route_geojson"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="render_map",
                title="Render Printable Topographic PDF & Device Exports",
                description=(
                    "Submit a GridForge map render job (true-scale local UTM PDF/GeoPDF, multi-sheet atlas, "
                    "strip atlas, ATAK MBTiles, Garmin KMZ, GeoPackage, OsmAnd SQLiteDB, or Field Bundle ZIP). "
                    "Waits synchronously by default (wait=true) and returns clickable signed downloads, "
                    "Studio share link, page 1 PNG preview, and the interactive Sheet Viewer."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "string", "description": "Center as 'lat,lon', MGRS string, or place name."},
                        "scale": {"type": "integer", "enum": scale_enum, "default": 25000},
                        "paper": {"type": "string", "enum": paper_enum, "default": "a4"},
                        "orientation": {"type": "string", "enum": orient_enum, "default": "portrait"},
                        "color_mode": {"type": "string", "enum": mode_enum, "default": "color"},
                        "units": {"type": "string", "enum": units_enum, "default": "metric"},
                        "title": {"type": "string"},
                        "date_iso": {"type": "string"},
                        "dpi": {"type": "integer", "minimum": 72, "maximum": 600, "default": 300},
                        "dem_source": {"type": "string", "enum": dem_enum, "default": "auto"},
                        "pack_id": {"type": "string"},
                        "us_overlays": {"type": "array", "items": {"type": "string", "enum": [o.value for o in USOverlayKind]}},
                        "au_overlays": {"type": "array", "items": {"type": "string", "enum": [o.value for o in AUOverlayKind]}},
                        "nz_overlays": {"type": "array", "items": {"type": "string", "enum": [o.value for o in NZOverlayKind]}},
                        "ca_overlays": {"type": "array", "items": {"type": "string", "enum": [o.value for o in CAOverlayKind]}},
                        "carto": {"type": "object", "description": "CartoOptions overrides (contour_density, hillshade_strength, label_density, show_slope_shading, etc.)."},
                        "outputs": {"type": "object", "description": "OutputOptions (pdf, geopdf, mbtiles, kmz, gpkg_tiles, osmand_sqlitedb, field_bundle_zip, etc.)."},
                        "grid_cols": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                        "grid_rows": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                        "waypoints": {"type": "array", "items": {"type": "object"}},
                        "viewsheds": {"type": "array", "items": {"type": "object"}},
                        "rf_links": {"type": "array", "items": {"type": "object"}},
                        "tactical_graphics": {"type": "array", "items": {"type": "object"}},
                        "strip_sheets": {"type": "array", "items": {"type": "object"}},
                        "include_calibration_page": {"type": "boolean", "default": False},
                        "include_route_card": {"type": "boolean", "default": False},
                        "include_elevation_profile": {"type": "boolean", "default": False},
                        "include_atlas_index": {"type": "boolean", "default": False},
                        "include_rf_link_card": {"type": "boolean", "default": False},
                        "show_fold_marks": {"type": "boolean", "default": False},
                        "wait": {"type": "boolean", "default": True, "description": "Wait for job completion before returning."},
                        "timeout_s": {"type": "number", "minimum": 1, "maximum": 300, "default": 120.0},
                        "include_preview_image": {"type": "boolean", "default": True},
                    },
                },
                annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="get_job_status",
                title="Get Render Job Status",
                description="Check status, progress stage, page count, and signed download URLs for an existing render job.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "GridForge job ID."},
                        "include_preview_image": {"type": "boolean", "default": False},
                    },
                    "required": ["job_id"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="get_job_page_preview",
                title="Inspect Rendered PDF Page PNG",
                description=(
                    "Fetch a rendered PNG preview of any page (1-based) of a completed map PDF job "
                    "(e.g. page 1 map sheet, page 2 RF Link Card, Route Card, Atlas Index, or 100mm Calibration Sheet)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "Completed GridForge job ID."},
                        "page": {"type": "integer", "minimum": 1, "default": 1},
                        "width": {"type": "integer", "minimum": 240, "maximum": 1600, "default": 900},
                    },
                    "required": ["job_id"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            ),
            MCPToolDefinition(
                name="create_share_link",
                title="Create Shareable Studio Snapshot Link",
                description="Persist a read-only GridForge Studio map state snapshot and return a shareable /?share=<id> URL.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "default": ""},
                        "notes": {"type": "string", "default": ""},
                        "state": {"type": "object", "description": "Studio map state JSON (center, scale, paper, color_mode, waypoints, etc.)."},
                        "project_id": {"type": "string", "description": "Optional saved project_id to snapshot instead of raw state."},
                        "expires_in_hours": {"type": "integer", "minimum": 1, "maximum": 8760},
                        "center": {"type": "string", "description": "Convenience shorthand if state is omitted."},
                        "scale": {"type": "integer", "enum": scale_enum},
                        "paper": {"type": "string", "enum": paper_enum},
                        "orientation": {"type": "string", "enum": orient_enum},
                        "color_mode": {"type": "string", "enum": mode_enum},
                    },
                },
                annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
            ),
            MCPToolDefinition(
                name="list_packs",
                title="List Installed Offline Packs & Regions",
                description="List installed air-gapped offline regional packs and the 29-region global build catalog (AU, NZ, North America).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "group": {
                            "type": "string",
                            "enum": ["au_tactical", "au_state", "nz", "na"],
                            "description": "Optional filter for catalog regions.",
                        },
                    },
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=INLINE_UI,
            ),
            MCPToolDefinition(
                name="get_capabilities",
                title="Get GridForge Server Capabilities",
                description="Inspect GridForge version, supported scales, paper sizes, 4 cartography themes, 6 DEM tiers, regional overlays, RF presets, and caller role limits.",
                input_schema={"type": "object", "properties": {}},
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            ),
            # App-only tools (SEP-1865 visibility: ["app"]) — invoked by ui://gridforge/app.html via app.callServerTool()
            MCPToolDefinition(
                name="app_get_job_status",
                title="Poll Job Status (App UI)",
                description="Internal MCP App helper for polling live render job progress and PDF page metadata.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=APP_ONLY,
            ),
            MCPToolDefinition(
                name="app_get_job_page",
                title="Fetch High-Res PDF Page PNG (App UI)",
                description="Internal MCP App helper returning base64 PNG data in structuredContent for the interactive Sheet Viewer without entering LLM context.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "page": {"type": "integer", "minimum": 1, "default": 1},
                        "width": {"type": "integer", "minimum": 240, "maximum": 2400, "default": 1100},
                    },
                    "required": ["job_id"],
                },
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=APP_ONLY,
            ),
            MCPToolDefinition(
                name="app_get_capabilities",
                title="Fetch Capabilities & Packs (App UI)",
                description="Internal MCP App helper populating interactive dropdown controls with live server capabilities and installed packs.",
                input_schema={"type": "object", "properties": {}},
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                meta=APP_ONLY,
            ),
        )

    @staticmethod
    def _build_resource_definitions() -> tuple[MCPResourceDefinition, ...]:
        return (
            MCPResourceDefinition(
                uri=APP_URI,
                name="gridforge_mcp_app",
                title="GridForge Interactive MCP App UI",
                description="Self-contained SEP-1865 interactive map explorer, RF Fresnel chart, and printable PDF sheet viewer.",
                mime_type=APP_MIME_TYPE,
                meta=APP_RESOURCE_META,
            ),
            MCPResourceDefinition(
                uri=CAPABILITIES_URI,
                name="gridforge_capabilities",
                title="GridForge Capabilities JSON",
                description="Live server scales, paper sizes, themes, DEM sources, regional overlays, and output formats.",
                mime_type="application/json",
            ),
            MCPResourceDefinition(
                uri=GUIDE_URI,
                name="gridforge_guide",
                title="GridForge Tactical Cartography Guide",
                description="Concise guide to selecting regional DEMs, overlays, themes, and workflows.",
                mime_type="text/markdown",
            ),
        )

    @staticmethod
    def _build_prompt_definitions() -> tuple[MCPPromptDefinition, ...]:
        return (
            MCPPromptDefinition(
                name="plan_backcountry_map",
                title="Plan Backcountry Topographic Map",
                description="Brief a backcountry location, pick the optimal DEM/overlays/theme, and prepare a true-scale printable PDF.",
                arguments=(
                    MCPPromptArgument("location", "Place name, 'lat,lon', or MGRS coordinate.", required=True),
                    MCPPromptArgument("activity", "Activity (e.g. hiking, ski-touring, SAR, bushwalking).", required=False),
                    MCPPromptArgument("scale", "Preferred scale denominator (default 25000).", required=False),
                ),
            ),
            MCPPromptDefinition(
                name="analyze_lora_link",
                title="Analyze LoRa / RF Point-to-Point Link",
                description="Evaluate a curved-Earth 1st Fresnel RF link between two sites and optionally render a printed RF Link Card.",
                arguments=(
                    MCPPromptArgument("from_location", "Transmitter place name or 'lat,lon'.", required=True),
                    MCPPromptArgument("to_location", "Receiver place name or 'lat,lon'.", required=True),
                    MCPPromptArgument("freq_mhz", "Frequency in MHz (default 915).", required=False),
                    MCPPromptArgument("antenna_height_m", "Antenna height AGL in meters (default 2).", required=False),
                ),
            ),
            MCPPromptDefinition(
                name="navigation_briefing",
                title="Tactical Navigation & Declination Briefing",
                description="Provide 10m MGRS, UTM zone, WMM2025 magnetic declination, grid convergence, and G-M angle for a location.",
                arguments=(
                    MCPPromptArgument("location", "Place name, 'lat,lon', or MGRS coordinate.", required=True),
                    MCPPromptArgument("question", "Optional navigation question.", required=False),
                ),
            ),
            MCPPromptDefinition(
                name="plan_strip_atlas",
                title="Plan Multi-Sheet Route Strip Atlas",
                description="Fit minimum-count overlapping true-scale UTM sheets along a trail or corridor and prepare an Atlas Index + Elevation Profile.",
                arguments=(
                    MCPPromptArgument("route_description", "Trail name, waypoints, or GeoJSON route description.", required=True),
                    MCPPromptArgument("scale", "Preferred scale denominator (default 25000).", required=False),
                    MCPPromptArgument("paper", "Paper size (a4, letter, legal, a3; default a4).", required=False),
                ),
            ),
        )

    # ------------------------------------------------------------------
    # App HTML loading & HTTP backend client
    # ------------------------------------------------------------------

    def load_app_html(self) -> str:
        """Load the SEP-1865 MCP App HTML bundle with live-override -> bundled -> fallback precedence."""
        candidates: list[Path] = []
        if self._app_html_path is not None:
            candidates.append(self._app_html_path)
        env_html = os.environ.get("GRIDFORGE_MCP_APP_HTML", "").strip()
        if env_html:
            candidates.append(Path(env_html))
        candidates.append(Path(__file__).resolve().parent / "assets" / "mcp_app.html")

        for path in candidates:
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except OSError:
                continue
        return _FALLBACK_APP_HTML

    def _effective_public_url(self, override: str | None = None) -> str | None:
        if override and override.strip():
            return override.strip().rstrip("/")
        if self._public_url:
            return self._public_url
        env_pub = os.environ.get("GRIDFORGE_PUBLIC_URL", "").strip()
        if env_pub:
            return env_pub.rstrip("/")
        if self._base_url:
            return self._base_url
        return None

    def _build_client(self, extra_headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        headers: dict[str, str] = {"Accept": "application/json"}
        forwarded = dict(extra_headers or {})
        has_auth = any(k.lower() in {"authorization", "x-api-key"} for k in forwarded)
        if not has_auth and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        for k, v in forwarded.items():
            if k.lower() in {"authorization", "x-api-key"} and v:
                headers[k] = v

        target_app = self._get_app() if self._get_app is not None else self._app
        if target_app is not None:
            transport = httpx.ASGITransport(app=target_app)
            return httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
                headers=headers,
                timeout=self._http_timeout_s,
            )
        assert self._base_url is not None
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=self._http_timeout_s,
        )

    async def _api_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = await client.request(method, path, params=params, json=json_body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} failed ({resp.status_code}): {_extract_error_detail(resp)}")
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}

    async def _fetch_capabilities_safe(self, client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            resp = await client.get("/api/capabilities")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        packs_count = 0
        try:
            presp = await client.get("/api/packs")
            if presp.status_code == 200:
                packs_count = len((presp.json() or {}).get("packs") or [])
        except Exception:
            pass
        return _static_capabilities_fallback(packs_count=packs_count)

    async def _fetch_packs_safe(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        try:
            resp = await client.get("/api/packs")
            if resp.status_code == 200:
                return list((resp.json() or {}).get("packs") or [])
        except Exception:
            pass
        return []

    async def _resolve_location_to_center(
        self,
        client: httpx.AsyncClient,
        location: str,
    ) -> tuple[str, str | None]:
        """Resolve a place name, ``'lat,lon'``, or MGRS string to ``(center_str, place_name)``."""
        raw = str(location or "").strip()
        if not raw:
            raise ValueError("Location must be a non-empty place name, 'lat,lon', or MGRS coordinate.")
        try:
            lat, lon, canonical_mgrs = parse_center(raw)
            if canonical_mgrs:
                return (canonical_mgrs, None)
            return (f"{lat:.6f},{lon:.6f}", None)
        except ValueError:
            pass

        geo = await self._api_json(client, "GET", "/api/geocode", params={"q": raw, "limit": 5})
        results = list(geo.get("results") or [])
        if not results:
            raise ValueError(
                f"Could not geocode '{raw}'. Try a more specific place name, decimal 'lat,lon', or MGRS coordinate."
            )
        top = results[0]
        center_str = f"{float(top['lat']):.6f},{float(top['lon']):.6f}"
        place_name = str(top.get("short_name") or top.get("display_name") or raw)
        return (center_str, place_name)

    def _enrich_job_urls(self, job: dict[str, Any], public_url: str | None) -> dict[str, Any]:
        out = dict(job)
        enriched_artifacts: list[dict[str, Any]] = []
        for art in out.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            item = dict(art)
            if item.get("download_url"):
                item["download_url"] = _absolutize_url(str(item["download_url"]), public_url)
            if item.get("signed_download_url"):
                item["signed_download_url"] = _absolutize_url(str(item["signed_download_url"]), public_url)
            enriched_artifacts.append(item)
        out["artifacts"] = enriched_artifacts
        return out

    async def _fetch_job_pages_safe(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        job: dict[str, Any],
        public_url: str | None,
    ) -> tuple[int, list[dict[str, Any]]]:
        try:
            resp = await client.get(f"/api/jobs/{quote(job_id, safe='')}/pages")
            if resp.status_code == 200:
                data = resp.json()
                raw_pages = list(data.get("pages") or [])
                pages: list[dict[str, Any]] = []
                for p in raw_pages:
                    if not isinstance(p, dict):
                        continue
                    item = dict(p)
                    if item.get("preview_url"):
                        item["preview_url"] = _absolutize_url(str(item["preview_url"]), public_url)
                    if item.get("signed_preview_url"):
                        item["signed_preview_url"] = _absolutize_url(str(item["signed_preview_url"]), public_url)
                    pages.append(item)
                return (int(data.get("page_count") or len(pages)), pages)
        except Exception:
            pass

        has_pdf = any(isinstance(a, dict) and a.get("kind") == "pdf" for a in (job.get("artifacts") or []))
        if has_pdf:
            url = _absolutize_url(f"/api/jobs/{job_id}/thumbnail.png", public_url)
            return (1, [{"page": 1, "preview_url": url}])
        return (0, [])

    async def _fetch_job_page_png_bytes(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        *,
        page: int = 1,
        width: int = 720,
    ) -> bytes | None:
        safe_id = quote(job_id, safe="")
        resp = await client.get(
            f"/api/jobs/{safe_id}/pages/{int(page)}.png",
            params={"width": int(width)},
            headers={"Accept": "image/png"},
        )
        if resp.status_code == 200 and resp.content.startswith(b"\x89PNG"):
            return resp.content
        if page == 1 and resp.status_code == 404:
            thumb = await client.get(
                f"/api/jobs/{safe_id}/thumbnail.png",
                headers={"Accept": "image/png"},
            )
            if thumb.status_code == 200 and thumb.content.startswith(b"\x89PNG"):
                return thumb.content
        return None

    # ------------------------------------------------------------------
    # JSON-RPC 2.0 Dispatch
    # ------------------------------------------------------------------

    async def handle_payload(
        self,
        payload: Any,
        *,
        request_headers: dict[str, str] | None = None,
        public_url: str | None = None,
    ) -> Any:
        """Handle a single JSON-RPC 2.0 object or a batch array; returns ``None`` for notifications."""
        if isinstance(payload, list):
            if not payload:
                return jsonrpc_error(None, INVALID_REQUEST, "Empty JSON-RPC batch.")
            responses: list[dict[str, Any]] = []
            for item in payload:
                resp = await self.handle_message(
                    item,
                    request_headers=request_headers,
                    public_url=public_url,
                )
                if resp is not None:
                    responses.append(resp)
            return responses if responses else None
        return await self.handle_message(
            payload,
            request_headers=request_headers,
            public_url=public_url,
        )

    async def handle_message(
        self,
        message: Any,
        *,
        request_headers: dict[str, str] | None = None,
        public_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Process a single JSON-RPC 2.0 request or notification."""
        if not isinstance(message, dict):
            return jsonrpc_error(None, INVALID_REQUEST, "JSON-RPC message must be an object.")

        req_id = message.get("id")
        is_notification = "id" not in message
        method = message.get("method")
        if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(method, str) or not method:
            if is_notification:
                return None
            return jsonrpc_error(req_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request envelope.")

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if is_notification:
                return None
            return jsonrpc_error(req_id, INVALID_PARAMS, "JSON-RPC 'params' must be an object.")

        # Notifications return None (202 Accepted on HTTP, no output on stdio)
        if method in {"notifications/initialized", "notifications/cancelled"} or method.startswith("notifications/"):
            return None

        try:
            result = await self._dispatch_method(
                method,
                params,
                request_headers=request_headers,
                public_url=public_url,
            )
            if is_notification:
                return None
            return jsonrpc_result(req_id, result)
        except MCPError as exc:
            if is_notification:
                return None
            return jsonrpc_error(req_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            logger.exception("Unhandled MCP error for method '%s': %s", method, exc)
            if is_notification:
                return None
            return jsonrpc_error(req_id, -32603, f"Internal server error: {exc}")

    async def _dispatch_method(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_headers: dict[str, str] | None = None,
        public_url: str | None = None,
    ) -> dict[str, Any]:
        if method == "initialize":
            return build_initialize_result(
                server_name=_SERVER_NAME,
                server_version=__version__,
                requested_protocol_version=params.get("protocolVersion"),
                instructions=_SERVER_INSTRUCTIONS,
                supports_ui_apps=True,
            )
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.to_dict() for t in self._tools]}
        if method == "tools/call":
            return await self._handle_tool_call(
                params,
                request_headers=request_headers,
                public_url=public_url,
            )
        if method == "resources/list":
            return {"resources": [r.to_dict() for r in self._resources]}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            return await self._handle_resource_read(
                params,
                request_headers=request_headers,
            )
        if method == "prompts/list":
            return {"prompts": [p.to_dict() for p in self._prompts]}
        if method == "prompts/get":
            return self._handle_prompt_get(params)

        raise MCPError(METHOD_NOT_FOUND, f"Unknown MCP method: '{method}'.")

    # ------------------------------------------------------------------
    # Resources & Prompts
    # ------------------------------------------------------------------

    async def _handle_resource_read(
        self,
        params: dict[str, Any],
        *,
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        uri = str(params.get("uri") or "").strip()
        if not uri:
            raise MCPError(INVALID_PARAMS, "Missing required 'uri' parameter.")
        if uri == APP_URI:
            return resource_read_result(
                APP_URI,
                mime_type=APP_MIME_TYPE,
                text=self.load_app_html(),
                meta=APP_RESOURCE_META,
            )
        if uri == CAPABILITIES_URI:
            async with self._build_client(request_headers) as client:
                caps = await self._fetch_capabilities_safe(client)
            return resource_read_result(
                CAPABILITIES_URI,
                mime_type="application/json",
                text=json.dumps(caps, indent=2),
            )
        if uri == GUIDE_URI:
            return resource_read_result(
                GUIDE_URI,
                mime_type="text/markdown",
                text=_GUIDE_MARKDOWN,
            )
        raise MCPError(INVALID_PARAMS, f"Unknown resource URI: '{uri}'.")

    def _handle_prompt_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise MCPError(INVALID_PARAMS, "Prompt 'arguments' must be an object.")

        if name == "plan_backcountry_map":
            loc = str(args.get("location") or "<location>").strip()
            activity = str(args.get("activity") or "backcountry navigation").strip()
            scale = str(args.get("scale") or "25000").strip()
            return {
                "description": f"Plan a true-scale backcountry topographic map for {loc}.",
                "messages": [
                    {
                        "role": "user",
                        "content": text_content(
                            f"Plan a printable true-scale MGRS topographic map for {loc} "
                            f"(activity: {activity}, preferred scale 1:{scale}). "
                            "1) Call `answer_location_briefing` to verify 10m MGRS, UTM zone, WMM2025 declination, "
                            "footprint coverage, and regional open-data DEM/overlay recommendations. "
                            "2) Recommend the best cartography theme (`color`, `alpine`, `bw`, or `night-red`) "
                            "and whether slope shading or a calibration/route card should be enabled. "
                            "3) Offer to generate the printable PDF (or ATAK/Garmin export) via `render_map`."
                        ),
                    }
                ],
            }
        if name == "analyze_lora_link":
            src = str(args.get("from_location") or "<from_location>").strip()
            dst = str(args.get("to_location") or "<to_location>").strip()
            freq = str(args.get("freq_mhz") or "915").strip()
            ant_h = str(args.get("antenna_height_m") or "2").strip()
            return {
                "description": f"Analyze {freq} MHz RF/Fresnel line-of-sight from {src} to {dst}.",
                "messages": [
                    {
                        "role": "user",
                        "content": text_content(
                            f"Analyze a {freq} MHz RF link from {src} to {dst} with {ant_h} m AGL antennas. "
                            "1) Resolve coordinates with `geocode_place` if either site is a place name. "
                            "2) Call `analyze_rf_link` to evaluate curved-Earth LOS, 60% 1st Fresnel clearance, "
                            "FSPL dB, and True/Grid/Magnetic bearings + NATO mils. "
                            "3) Summarize the verdict and offer to render a map with `include_rf_link_card=true`."
                        ),
                    }
                ],
            }
        if name == "navigation_briefing":
            loc = str(args.get("location") or "<location>").strip()
            q = str(args.get("question") or "").strip()
            q_part = f" Answer specifically: {q}" if q else ""
            return {
                "description": f"Tactical MGRS, UTM, and WMM2025 declination briefing for {loc}.",
                "messages": [
                    {
                        "role": "user",
                        "content": text_content(
                            f"Provide a complete tactical navigation briefing for {loc} using `answer_location_briefing` "
                            "(10m MGRS, UTM zone/EPSG, 100 km square ID, WMM2025 magnetic declination, grid convergence, "
                            f"G-M angle in degrees and NATO mils, and recommended open-data overlays).{q_part}"
                        ),
                    }
                ],
            }
        if name == "plan_strip_atlas":
            desc = str(args.get("route_description") or "<route>").strip()
            scale = str(args.get("scale") or "25000").strip()
            paper = str(args.get("paper") or "a4").strip()
            return {
                "description": f"Plan a multi-sheet true-scale strip atlas for {desc}.",
                "messages": [
                    {
                        "role": "user",
                        "content": text_content(
                            f"Plan a true-scale strip atlas for {desc} at 1:{scale} on {paper.upper()} paper. "
                            "1) Assemble ordered route coordinates or GeoJSON LineString. "
                            "2) Call `plan_strip_atlas` to minimize overlapping sheet count and `measure_route` for leg azimuths. "
                            "3) Offer to call `render_map` with `strip_sheets`, `include_atlas_index=true`, and `include_elevation_profile=true`."
                        ),
                    }
                ],
            }
        raise MCPError(INVALID_PARAMS, f"Unknown prompt: '{name}'.")

    # ------------------------------------------------------------------
    # Tool Dispatcher & Handlers
    # ------------------------------------------------------------------

    async def _handle_tool_call(
        self,
        params: dict[str, Any],
        *,
        request_headers: dict[str, str] | None = None,
        public_url: str | None = None,
    ) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        if name not in self._tool_map:
            raise MCPError(INVALID_PARAMS, f"Unknown tool: '{name}'.")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise MCPError(INVALID_PARAMS, "Tool 'arguments' must be a JSON object.")

        eff_public_url = self._effective_public_url(public_url)
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise MCPError(METHOD_NOT_FOUND, f"Handler not implemented for tool '{name}'.")

        try:
            async with self._build_client(request_headers) as client:
                return await handler(client, args, eff_public_url)
        except MCPError:
            raise
        except Exception as exc:
            return call_tool_result(
                [text_content(f"Error executing `{name}`: {exc}")],
                is_error=True,
            )

    async def _tool_open_gridforge_studio(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        caps = await self._fetch_capabilities_safe(client)
        packs = await self._fetch_packs_safe(client)
        location = str(args.get("location") or "").strip() or None
        scale = int(args.get("scale") or 25000)
        paper = str(args.get("paper") or "a4").lower()
        color_mode = str(args.get("color_mode") or "color").lower()

        center_str: str | None = None
        if location:
            try:
                center_str, _ = await self._resolve_location_to_center(client, location)
            except Exception:
                center_str = None

        studio_url = build_studio_url(
            public_url,
            center=center_str,
            scale=scale if center_str else None,
            paper=paper if center_str else None,
            color_mode=color_mode if center_str else None,
        )
        title = "GridForge Tactical Topographic Studio"
        summary = (
            f"GridForge v{caps.get('version', __version__)} ready — {len(_STUDIO_PRESETS)} global presets, "
            f"{len(packs)} installed offline pack(s), 4 cartography themes, and 7 true-scale UTM ratios."
        )
        structured = {
            "view": "studio",
            "title": title,
            "summary": summary,
            "presets": [dict(p) for p in _STUDIO_PRESETS],
            "capabilities": caps,
            "packs": packs,
            "public_url": public_url,
            "share_url": studio_url,
        }
        lines = [
            f"**{title}**",
            summary,
            "Search any location, pick a regional preset, inspect MGRS/declination or RF links, or render a printable PDF.",
        ]
        if studio_url:
            lines.append(f"Open in browser: {studio_url}")
        return call_tool_result([text_content("\n".join(lines))], structured_content=structured)

    async def _build_briefing_response(
        self,
        client: httpx.AsyncClient,
        *,
        center_str: str,
        place_name: str | None,
        question: str | None,
        scale: int,
        paper: str,
        orientation: str,
        color_mode: str,
        units: str,
        grid_cols: int,
        grid_rows: int,
        date_iso: str | None,
        title_override: str | None,
        waypoints: list[dict[str, Any]] | None,
        include_image: bool,
        public_url: str | None,
    ) -> dict[str, Any]:
        fp_req: dict[str, Any] = {
            "center": center_str,
            "scale": scale,
            "paper": paper,
            "orientation": orientation,
            "units": units,
            "grid_cols": grid_cols,
            "grid_rows": grid_rows,
        }
        if date_iso:
            fp_req["date_iso"] = date_iso

        fp = await self._api_json(client, "POST", "/api/footprint", json_body=fp_req)
        packs = await self._fetch_packs_safe(client)

        lat = float(fp["center_lat"])
        lon = float(fp["center_lon"])
        rec_raw = _recommend_regional_data(lat, lon, packs)
        rec = {
            "dem_source": rec_raw["dem_source"],
            "region_label": rec_raw["region_label"],
            "overlays": rec_raw["overlays"],
            "matching_packs": rec_raw["matching_packs"],
        }
        decl = fp["declination"]

        label = title_override or place_name or fp["center_mgrs_10m"]
        sheet_count = int(fp["sheet_count"])
        footprint_desc = (
            f"{sheet_count} sheets ({fp['grid_cols']}x{fp['grid_rows']}), "
            f"{fp['total_ground_width_km']:.2f} × {fp['total_ground_height_km']:.2f} km total"
            if sheet_count > 1
            else f"{fp['ground_width_km']:.2f} × {fp['ground_height_km']:.2f} km"
        )
        overlay_str = ", ".join(rec["overlays"]) if rec["overlays"] else "OpenStreetMap base vectors"
        pack_str = (
            f" Matching offline pack(s): {', '.join(rec['matching_packs'])}."
            if rec["matching_packs"]
            else ""
        )

        answer_parts = [
            f"**{label}** (`{lat:.5f}, {lon:.5f}`)",
            f"- **MGRS (10 m):** `{fp['center_mgrs_10m']}` (GZD `{fp['gzd']}`, 100 km square `{', '.join(fp['square_ids_100km'])}`)",
            f"- **Local UTM Projection:** Zone `{fp['utm_zone']}{fp['utm_hemisphere']}` (`EPSG:{fp['utm_epsg']}`)",
            (
                f"- **WMM2025 North Angles ({decl['decimal_year']:.2f}):** "
                f"Magnetic Declination `{_format_ew(decl['magnetic_declination_deg'])}` "
                f"({decl['annual_change_deg']:+.3f}°/yr), "
                f"Grid Convergence `{_format_ew(decl['grid_convergence_deg'])}`, "
                f"G-M Angle `{_format_ew(decl['gm_angle_deg'])}` (`{abs(decl['gm_angle_mils']):.0f}` NATO mils)"
            ),
            (
                f"- **True-Scale Sheet:** `1:{scale:,}` {paper.upper()} {orientation} (`{color_mode}`) — "
                f"{footprint_desc} (1 km MGRS = `{fp['grid_spacing_mm']:.1f} mm` on paper)"
            ),
            f"- **Recommended Open Data ({rec['region_label']}):** DEM `{rec['dem_source']}`, overlays `{overlay_str}`.{pack_str}",
        ]
        if question:
            answer_parts.insert(0, f"Question: {question}")

        # Build Studio deep link
        ov_kwargs: dict[str, Any] = {}
        ov_key = rec_raw.get("overlay_param")
        if ov_key and rec["overlays"]:
            ov_kwargs[ov_key] = rec["overlays"]
        studio_url = build_studio_url(
            public_url,
            center=f"{lat:.6f},{lon:.6f}",
            scale=scale,
            paper=paper,
            orientation=orientation,
            color_mode=color_mode,
            units=units,
            dem_source=rec["dem_source"] if rec["dem_source"] != "glo30" else "auto",
            grid_cols=grid_cols,
            grid_rows=grid_rows,
            title=place_name or title_override,
            waypoints=waypoints,
            **ov_kwargs,
        )
        if studio_url:
            answer_parts.append(f"- **Open in Studio:** {studio_url}")

        answer_text = "\n".join(answer_parts)
        summary = (
            f"{fp['center_mgrs_10m']} · UTM {fp['utm_zone']}{fp['utm_hemisphere']} (EPSG:{fp['utm_epsg']}) · "
            f"Decl {_format_ew(decl['magnetic_declination_deg'])} · {footprint_desc}"
        )

        structured: dict[str, Any] = {
            "view": "briefing",
            "title": str(label),
            "summary": summary,
            "answer": answer_text,
            "location": {
                "center": f"{lat:.6f},{lon:.6f}",
                "lat": lat,
                "lon": lon,
                "mgrs_10m": fp["center_mgrs_10m"],
                "utm_epsg": fp["utm_epsg"],
                "utm_zone": fp["utm_zone"],
                "utm_hemisphere": fp["utm_hemisphere"],
                "gzd": fp["gzd"],
                "square_ids_100km": fp["square_ids_100km"],
                "place_name": place_name,
            },
            "sheet": {
                "scale": scale,
                "paper": paper,
                "orientation": orientation,
                "color_mode": color_mode,
                "units": units,
                "page_width_mm": fp["page_width_mm"],
                "page_height_mm": fp["page_height_mm"],
                "ground_width_km": fp["ground_width_km"],
                "ground_height_km": fp["ground_height_km"],
                "total_ground_width_km": fp["total_ground_width_km"],
                "total_ground_height_km": fp["total_ground_height_km"],
                "grid_cols": fp["grid_cols"],
                "grid_rows": fp["grid_rows"],
                "sheet_count": fp["sheet_count"],
                "grid_spacing_mm": fp["grid_spacing_mm"],
            },
            "declination": decl,
            "recommendations": rec,
            "footprint_geojson": fp["footprint_geojson"],
            "grid_geojson": fp["grid_geojson"],
            "studio_url": studio_url,
        }
        if question:
            structured["question"] = question
        if waypoints:
            structured["waypoints"] = waypoints

        content: list[dict[str, Any]] = [text_content(answer_text)]
        if include_image:
            png = render_footprint_card_png(
                {**fp, "scale": scale, "paper": paper, "orientation": orientation},
                title=str(label),
                waypoints=waypoints,
            )
            content.append(image_content(png))

        return call_tool_result(content, structured_content=structured)

    async def _tool_answer_location_briefing(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        location = str(args.get("location") or "").strip()
        center_str, place_name = await self._resolve_location_to_center(client, location)
        return await self._build_briefing_response(
            client,
            center_str=center_str,
            place_name=place_name,
            question=str(args["question"]).strip() if args.get("question") else None,
            scale=int(args.get("scale") or 25000),
            paper=str(args.get("paper") or "a4").lower(),
            orientation=str(args.get("orientation") or "portrait").lower(),
            color_mode=str(args.get("color_mode") or "color").lower(),
            units=str(args.get("units") or "metric").lower(),
            grid_cols=int(args.get("grid_cols") or 1),
            grid_rows=int(args.get("grid_rows") or 1),
            date_iso=str(args["date_iso"]).strip() if args.get("date_iso") else None,
            title_override=None,
            waypoints=None,
            include_image=bool(args.get("include_image", True)),
            public_url=public_url,
        )

    async def _tool_geocode_place(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        _ = public_url
        query = str(args.get("query") or "").strip()
        limit = max(1, min(10, int(args.get("limit") or 5)))
        if len(query) < 2:
            return call_tool_result([text_content("Provide a search query of at least 2 characters.")], is_error=True)

        data = await self._api_json(client, "GET", "/api/geocode", params={"q": query, "limit": limit})
        results = list(data.get("results") or [])
        title = f"Geocode: {query}"
        summary = f"Found {len(results)} match(es) for '{query}'."
        if results:
            lines = [summary] + [
                f"{idx}. **{r.get('short_name') or r.get('display_name')}** — `{r['lat']:.5f}, {r['lon']:.5f}` ({r.get('display_name')})"
                for idx, r in enumerate(results, start=1)
            ]
        else:
            lines = [f"No results found for '{query}' (or offline mode). Try passing 'lat,lon' or MGRS directly."]
        return call_tool_result(
            [text_content("\n".join(lines))],
            structured_content={
                "view": "geocode",
                "title": title,
                "summary": summary,
                "query": query,
                "results": results,
            },
        )

    async def _tool_preview_footprint(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        center = str(args.get("center") or "").strip()
        center_str, place_name = await self._resolve_location_to_center(client, center)
        return await self._build_briefing_response(
            client,
            center_str=center_str,
            place_name=place_name,
            question=None,
            scale=int(args.get("scale") or 25000),
            paper=str(args.get("paper") or "a4").lower(),
            orientation=str(args.get("orientation") or "portrait").lower(),
            color_mode=str(args.get("color_mode") or "color").lower(),
            units=str(args.get("units") or "metric").lower(),
            grid_cols=int(args.get("grid_cols") or 1),
            grid_rows=int(args.get("grid_rows") or 1),
            date_iso=str(args["date_iso"]).strip() if args.get("date_iso") else None,
            title_override=str(args["title"]).strip() if args.get("title") else None,
            waypoints=list(args.get("waypoints") or []) or None,
            include_image=bool(args.get("include_image", True)),
            public_url=public_url,
        )

    async def _tool_analyze_rf_link(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        req_body: dict[str, Any] = {
            "from_lat": float(args["from_lat"]),
            "from_lon": float(args["from_lon"]),
            "to_lat": float(args["to_lat"]),
            "to_lon": float(args["to_lon"]),
            "freq_mhz": float(args.get("freq_mhz") or 915.0),
            "from_height_m": float(args.get("from_height_m", 2.0)),
            "to_height_m": float(args.get("to_height_m", 2.0)),
            "k_factor": float(args.get("k_factor", 4.0 / 3.0)),
            "from_label": str(args.get("from_label") or "TX"),
            "to_label": str(args.get("to_label") or "RX"),
            "dem_source": str(args.get("dem_source") or "auto"),
        }
        if args.get("pack_id"):
            req_body["pack_id"] = str(args["pack_id"])
        if args.get("date_iso"):
            req_body["date_iso"] = str(args["date_iso"])

        profile = await self._api_json(client, "POST", "/api/rf-link-profile", json_body=req_body)

        mid_lat = 0.5 * (req_body["from_lat"] + req_body["to_lat"])
        mid_lon = 0.5 * (req_body["from_lon"] + req_body["to_lon"])
        studio_url = build_studio_url(
            public_url,
            center=f"{mid_lat:.6f},{mid_lon:.6f}",
            scale=25000,
            rf_links=[req_body],
            include_rf_link_card=True,
        )

        status = str(profile["status"]).upper()
        clr = float(profile["min_clearance_m"])
        clr_s = f"+{clr:.1f} m" if clr >= 0 else f"{clr:.1f} m"
        title = f"RF Link: {profile.get('from_label', 'TX')} → {profile.get('to_label', 'RX')} ({status})"
        summary = (
            f"{status} · {profile['distance_km']:.2f} km @ {profile['freq_mhz']:g} MHz · "
            f"FSPL {profile['fspl_db']:.1f} dB · Min Clr {clr_s} ({profile['min_fresnel_pct']:.0f}% F1)"
        )
        answer_lines = [
            f"**{title}**",
            f"- **Verdict:** `{status}` (LOS clear: `{profile['los_clear']}`, 60% 1st Fresnel clear: `{profile['fresnel_60pct_clear']}`)",
            f"- **Link Budget:** `{profile['distance_km']:.2f} km` at `{profile['freq_mhz']:g} MHz` — Free-Space Path Loss `{profile['fspl_db']:.1f} dB`, Min Clearance `{clr_s}` (`{profile['min_fresnel_pct']:.0f}%` of 1st Fresnel zone)",
            f"- **Azimuths (TX→RX):** True `{profile['bearing_true_deg']:05.1f}°` · Grid `{profile['bearing_grid_deg']:05.1f}°` · Mag `{profile['bearing_mag_deg']:05.1f}°` (`{profile['bearing_mils']:.0f}` NATO mils)",
            f"- **Endpoints:** {profile['from_label']} `{profile['from_mgrs']}` ({profile['from_elev_m']:.0f} m + {profile['from_height_m']:g} m AGL) → {profile['to_label']} `{profile['to_mgrs']}` ({profile['to_elev_m']:.0f} m + {profile['to_height_m']:g} m AGL)",
        ]
        if studio_url:
            answer_lines.append(f"- **Open in Studio:** {studio_url}")
        answer_text = "\n".join(answer_lines)

        content: list[dict[str, Any]] = [text_content(answer_text)]
        if bool(args.get("include_image", True)):
            png = render_rf_link_chart_png(profile)
            content.append(image_content(png))

        structured = {
            "view": "rf_link",
            "title": title,
            "summary": summary,
            "answer": answer_text,
            "request": req_body,
            "profile": profile,
            "studio_url": studio_url,
        }
        return call_tool_result(content, structured_content=structured)

    async def _tool_compute_viewshed(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        observer = {
            "label": str(args.get("label") or "Observer"),
            "lat": float(args["lat"]),
            "lon": float(args["lon"]),
            "observer_height_m": float(args.get("observer_height_m", 2.0)),
            "target_height_m": float(args.get("target_height_m", 1.5)),
            "max_distance_m": float(args.get("max_distance_m", 10000.0)),
        }
        req_body: dict[str, Any] = {
            "observer": observer,
            "dem_source": str(args.get("dem_source") or "auto"),
        }
        if args.get("pack_id"):
            req_body["pack_id"] = str(args["pack_id"])

        vs = await self._api_json(client, "POST", "/api/viewshed-preview", json_body=req_body)

        studio_url = build_studio_url(
            public_url,
            center=f"{observer['lat']:.6f},{observer['lon']:.6f}",
            scale=50000 if observer["max_distance_m"] > 5000 else 25000,
            viewsheds=[observer],
        )

        radius_km = float(vs["max_distance_m"]) / 1000.0
        title = f"Viewshed: {observer['label']} ({radius_km:.1f} km radius)"
        summary = (
            f"Visible area {vs['visible_area_km2']:.2f} km² within {radius_km:.1f} km radius · "
            f"Observer {vs['observer_mgrs_10m']} @ {vs['observer_elev_m']:.0f} m"
        )
        answer_lines = [
            f"**{title}**",
            f"- **Observer:** `{vs['observer_mgrs_10m']}` (`{observer['lat']:.5f}, {observer['lon']:.5f}`), elevation `{vs['observer_elev_m']:.0f} m` (+`{observer['observer_height_m']:g} m` AGL, target `{observer['target_height_m']:g} m`)",
            f"- **Coverage:** `{vs['visible_area_km2']:.2f} km²` visible within `{radius_km:.1f} km` radius (`EPSG:{vs['utm_epsg']}`)",
        ]
        if studio_url:
            answer_lines.append(f"- **Open in Studio:** {studio_url}")
        answer_text = "\n".join(answer_lines)

        content: list[dict[str, Any]] = [text_content(answer_text)]
        if bool(args.get("include_image", True)):
            # Fetch lightweight footprint for declination compass & MGRS grid backdrop
            fp_backdrop: dict[str, Any] = {
                "center_lat": observer["lat"],
                "center_lon": observer["lon"],
                "center_mgrs_10m": vs["observer_mgrs_10m"],
                "utm_epsg": vs["utm_epsg"],
            }
            try:
                fp_backdrop = await self._api_json(
                    client,
                    "POST",
                    "/api/footprint",
                    json_body={
                        "center": f"{observer['lat']:.6f},{observer['lon']:.6f}",
                        "scale": 50000 if observer["max_distance_m"] > 5000 else 25000,
                    },
                )
            except Exception:
                pass
            png = render_footprint_card_png(
                fp_backdrop,
                title=title,
                viewshed={**vs, "observer": observer},
            )
            content.append(image_content(png))

        structured = {
            "view": "viewshed",
            "title": title,
            "summary": summary,
            "answer": answer_text,
            "observer": observer,
            "result": vs,
            "studio_url": studio_url,
        }
        return call_tool_result(content, structured_content=structured)

    async def _tool_measure_route(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        waypoints_in = list(args.get("waypoints") or [])
        points_in = list(args.get("points") or [])
        units = str(args.get("units") or "metric").lower()
        date_iso = str(args["date_iso"]).strip() if args.get("date_iso") else None
        dem_source = str(args["dem_source"]).strip() if args.get("dem_source") else None
        pack_id = str(args["pack_id"]).strip() if args.get("pack_id") else None
        title_override = str(args["title"]).strip() if args.get("title") else None

        if not waypoints_in and len(points_in) < 2:
            return call_tool_result(
                [text_content("Provide either 'waypoints' (list of {label, lat, lon}) or 'points' (at least 2 coordinates).")],
                is_error=True,
            )

        route_metrics: dict[str, Any] | None = None
        ruler_metrics: dict[str, Any] | None = None
        normalized_points: list[dict[str, float]] = []

        if waypoints_in:
            rm_req: dict[str, Any] = {"waypoints": waypoints_in, "units": units}
            if date_iso:
                rm_req["date_iso"] = date_iso
            route_metrics = await self._api_json(client, "POST", "/api/route-metrics", json_body=rm_req)
            normalized_points = [
                {"lat": float(w["lat"]), "lon": float(w["lon"])}
                for w in (route_metrics.get("waypoints") or [])
            ]
            if (dem_source or pack_id) and len(normalized_points) >= 2:
                ru_req: dict[str, Any] = {"points": normalized_points}
                if dem_source:
                    ru_req["dem_source"] = dem_source
                if pack_id:
                    ru_req["pack_id"] = pack_id
                if date_iso:
                    ru_req["date_iso"] = date_iso
                ruler_metrics = await self._api_json(client, "POST", "/api/ruler-metrics", json_body=ru_req)
        else:
            for pt in points_in:
                if isinstance(pt, dict):
                    normalized_points.append({"lat": float(pt["lat"]), "lon": float(pt["lon"])})
                elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    normalized_points.append({"lat": float(pt[0]), "lon": float(pt[1])})
            ru_req = {"points": normalized_points}
            if dem_source:
                ru_req["dem_source"] = dem_source
            if pack_id:
                ru_req["pack_id"] = pack_id
            if date_iso:
                ru_req["date_iso"] = date_iso
            ruler_metrics = await self._api_json(client, "POST", "/api/ruler-metrics", json_body=ru_req)

        center_str = (
            f"{normalized_points[0]['lat']:.6f},{normalized_points[0]['lon']:.6f}"
            if normalized_points
            else None
        )
        studio_url = build_studio_url(
            public_url,
            center=center_str,
            scale=25000,
            waypoints=waypoints_in or None,
            include_route_card=bool(waypoints_in),
        )

        if route_metrics is not None:
            total_km = float(route_metrics.get("total_distance_km") or 0.0)
            total_mi = float(route_metrics.get("total_distance_mi") or 0.0)
            legs = list(route_metrics.get("legs") or [])
            title = title_override or f"Tactical Route ({len(route_metrics.get('waypoints') or [])} waypoints)"
            summary = f"{total_km:.2f} km ({total_mi:.2f} mi) across {len(legs)} leg(s)."
            lines = [f"**{title}** — `{total_km:.2f} km` (`{total_mi:.2f} mi`), {len(legs)} leg(s):"]
            for leg in legs:
                lines.append(
                    f"- {leg['from_code']} ({leg['from_label']}) → {leg['to_code']} ({leg['to_label']}): "
                    f"`{leg['leg_dist_km']:.2f} km`, True `{leg['true_az_deg']:05.1f}°`, "
                    f"Grid `{leg['grid_az_deg']:05.1f}°`, Mag `{leg['mag_az_deg']:05.1f}°` (`{leg['mag_az_mils']:.0f}` mil)"
                )
            card_input = dict(route_metrics)
            if ruler_metrics and ruler_metrics.get("segments"):
                card_input["segments"] = ruler_metrics["segments"]
        else:
            assert ruler_metrics is not None
            total_km = float(ruler_metrics.get("total_distance_m") or 0.0) / 1000.0
            segs = list(ruler_metrics.get("segments") or [])
            title = title_override or f"Ruler Measurement ({len(normalized_points)} points)"
            summary = f"{total_km:.2f} km across {len(segs)} segment(s)."
            lines = [f"**{title}** — `{total_km:.2f} km` (`{total_km * 0.621371:.2f} mi`), {len(segs)} segment(s):"]
            for seg in segs:
                grade_s = f", grade `{seg['grade_pct']:+.1f}%`" if seg.get("grade_pct") is not None else ""
                lines.append(
                    f"- P{seg['from_idx'] + 1} → P{seg['to_idx'] + 1}: "
                    f"`{seg['distance_m'] / 1000.0:.2f} km`, True `{seg['bearing_true_deg']:05.1f}°`, "
                    f"Grid `{seg['bearing_grid_deg']:05.1f}°`, Mag `{seg['bearing_mag_deg']:05.1f}°` (`{seg['bearing_mils']:.0f}` mil){grade_s}"
                )
            card_input = {**ruler_metrics, "points": normalized_points}

        if studio_url:
            lines.append(f"- **Open in Studio:** {studio_url}")
        answer_text = "\n".join(lines)

        content: list[dict[str, Any]] = [text_content(answer_text)]
        if bool(args.get("include_image", True)):
            png = render_route_card_png(card_input, title=title)
            content.append(image_content(png))

        structured: dict[str, Any] = {
            "view": "route",
            "title": title,
            "summary": summary,
            "answer": answer_text,
            "waypoints": route_metrics.get("waypoints") if route_metrics else waypoints_in or None,
            "route_metrics": route_metrics,
            "ruler_metrics": ruler_metrics,
            "studio_url": studio_url,
        }
        return call_tool_result(content, structured_content=structured)

    async def _tool_plan_strip_atlas(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        req_body: dict[str, Any] = {
            "route_geojson": args["route_geojson"],
            "scale": int(args.get("scale") or 25000),
            "paper": str(args.get("paper") or "a4").lower(),
            "orientation": str(args.get("orientation") or "portrait").lower(),
            "units": str(args.get("units") or "metric").lower(),
            "overlap_pct": float(args.get("overlap_pct", 10.0)),
            "max_sheets": int(args.get("max_sheets") or 12),
        }
        plan = await self._api_json(client, "POST", "/api/strip-plan", json_body=req_body)

        sheets = list(plan.get("sheets") or [])
        center_str = (
            f"{sheets[0]['center_lat']:.6f},{sheets[0]['center_lon']:.6f}" if sheets else None
        )
        studio_url = build_studio_url(
            public_url,
            center=center_str,
            scale=req_body["scale"],
            paper=req_body["paper"],
            orientation=req_body["orientation"],
            include_atlas_index=True,
            include_elevation_profile=True,
        )

        title = str(args.get("title") or "").strip() or f"Strip Atlas Plan ({plan['sheet_count']} sheets @ 1:{plan['scale']:,})"
        summary = (
            f"{plan['route_distance_km']:.2f} km route fitted into {plan['sheet_count']} true-scale "
            f"{str(plan['paper']).upper()} {plan['orientation']} sheets in UTM {plan['utm_zone']}{plan['utm_hemisphere']} (EPSG:{plan['utm_epsg']})."
        )
        lines = [f"**{title}**", summary]
        for sh in sheets:
            lines.append(
                f"- **{sh['sheet_id']}**: `{sh['center_mgrs_10m']}` (`{sh['center_lat']:.5f}, {sh['center_lon']:.5f}`)"
            )
        if studio_url:
            lines.append(f"- **Open in Studio:** {studio_url}")
        answer_text = "\n".join(lines)

        content: list[dict[str, Any]] = [text_content(answer_text)]
        if bool(args.get("include_image", True)):
            png = render_route_card_png(plan, title=title)
            content.append(image_content(png))

        structured = {
            "view": "route",
            "title": title,
            "summary": summary,
            "answer": answer_text,
            "strip_plan": plan,
            "footprint_geojson": plan.get("footprint_geojson"),
            "grid_geojson": plan.get("grid_geojson"),
            "studio_url": studio_url,
        }
        return call_tool_result(content, structured_content=structured)

    async def _tool_render_map(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        raw_center = args.get("center")
        place_name: str | None = None
        if raw_center is not None and str(raw_center).strip():
            center_str, place_name = await self._resolve_location_to_center(client, str(raw_center))
            payload["center"] = center_str

        passthrough_keys = (
            "scale",
            "paper",
            "orientation",
            "color_mode",
            "units",
            "title",
            "date_iso",
            "dpi",
            "dem_source",
            "pack_id",
            "us_overlays",
            "au_overlays",
            "nz_overlays",
            "ca_overlays",
            "carto",
            "outputs",
            "grid_cols",
            "grid_rows",
            "waypoints",
            "viewsheds",
            "rf_links",
            "tactical_graphics",
            "strip_sheets",
            "include_calibration_page",
            "include_route_card",
            "include_elevation_profile",
            "include_atlas_index",
            "include_rf_link_card",
            "show_fold_marks",
        )
        for key in passthrough_keys:
            if key in args and args[key] is not None:
                payload[key] = args[key]

        if not payload.get("title") and place_name:
            payload["title"] = place_name[:80]

        wait = bool(args.get("wait", True))
        timeout_s = max(1.0, min(300.0, float(args.get("timeout_s", 120.0))))
        include_preview = bool(args.get("include_preview_image", True))

        job_raw = await self._api_json(
            client,
            "POST",
            "/api/jobs",
            params={"wait": "true" if wait else "false", "timeout_s": str(timeout_s)},
            json_body=payload,
        )
        job = self._enrich_job_urls(job_raw, public_url)
        job_id = str(job["job_id"])
        status_str = str(job.get("status") or "queued")

        page_count = 0
        pages: list[dict[str, Any]] = []
        preview_png: bytes | None = None
        if status_str == "succeeded":
            page_count, pages = await self._fetch_job_pages_safe(client, job_id, job, public_url)
            has_pdf = any(a.get("kind") == "pdf" for a in (job.get("artifacts") or []))
            if include_preview and has_pdf:
                raw_png = await self._fetch_job_page_png_bytes(client, job_id, page=1, width=720)
                if raw_png is not None:
                    preview_png = _fit_png_under_budget(raw_png)

        studio_url = build_studio_url(
            public_url,
            center=job.get("center"),
            scale=int(job.get("scale") or 25000),
            paper=str(job.get("paper") or "a4"),
            orientation=str(job.get("orientation") or "portrait"),
            color_mode=str(job.get("color_mode") or "color"),
            units=str(payload.get("units") or "metric"),
            dpi=int(payload.get("dpi") or 300),
            dem_source=str(payload.get("dem_source") or "auto"),
            pack_id=payload.get("pack_id"),
            grid_cols=int(job.get("grid_cols") or 1),
            grid_rows=int(job.get("grid_rows") or 1),
            title=job.get("title"),
            au_overlays=payload.get("au_overlays"),
            nz_overlays=payload.get("nz_overlays"),
            ca_overlays=payload.get("ca_overlays"),
            us_overlays=payload.get("us_overlays"),
            waypoints=payload.get("waypoints"),
            viewsheds=payload.get("viewsheds"),
            rf_links=payload.get("rf_links"),
            include_calibration_page=bool(payload.get("include_calibration_page")),
            include_route_card=bool(payload.get("include_route_card")),
            include_elevation_profile=bool(payload.get("include_elevation_profile")),
            include_atlas_index=bool(payload.get("include_atlas_index")),
            include_rf_link_card=bool(payload.get("include_rf_link_card")),
            show_fold_marks=bool(payload.get("show_fold_marks")),
        )

        # Best-effort share snapshot creation for clean clickable Studio link
        share_url: str | None = studio_url
        try:
            share_state: dict[str, Any] = {
                "center": job.get("center"),
                "title": job.get("title") or "",
                "scale": int(job.get("scale") or 25000),
                "paper": str(job.get("paper") or "a4"),
                "orientation": str(job.get("orientation") or "portrait"),
                "color_mode": str(job.get("color_mode") or "color"),
                "grid_cols": int(job.get("grid_cols") or 1),
                "grid_rows": int(job.get("grid_rows") or 1),
            }
            for opt_k in (
                "units",
                "dpi",
                "dem_source",
                "pack_id",
                "au_overlays",
                "nz_overlays",
                "ca_overlays",
                "us_overlays",
                "carto",
                "outputs",
                "waypoints",
                "viewsheds",
                "rf_links",
                "tactical_graphics",
                "include_calibration_page",
                "include_route_card",
                "include_elevation_profile",
                "include_atlas_index",
                "include_rf_link_card",
                "show_fold_marks",
            ):
                if opt_k in payload:
                    share_state[opt_k] = payload[opt_k]
            sresp = await client.post(
                "/api/shares",
                json={"name": str(job.get("title") or job.get("center_mgrs") or "GridForge Map")[:120], "state": share_state},
            )
            if sresp.status_code in (200, 201):
                spath = str((sresp.json() or {}).get("share_path") or "")
                if spath:
                    share_url = _absolutize_url(spath, public_url)
        except Exception:
            pass

        title = str(job.get("title") or job.get("center_mgrs") or f"Job {job_id}")
        summary = (
            f"Job `{job_id}` is **{status_str}** "
            f"(1:{int(job.get('scale') or 25000):,} {str(job.get('paper') or 'a4').upper()} {job.get('color_mode')})."
        )
        lines = [f"**{title}** — {summary}"]
        if job.get("error"):
            lines.append(f"- **Error:** {job['error']}")

        content: list[dict[str, Any]] = []
        resource_links: list[dict[str, Any]] = []
        for art in job.get("artifacts") or []:
            best_url = art.get("signed_download_url") or art.get("download_url")
            size_kb = float(art.get("size_bytes") or 0) / 1024.0
            lines.append(f"- **Download {str(art.get('kind', 'file')).upper()}**: [{art.get('filename')}]({best_url}) ({size_kb:.1f} KB)")
            if best_url:
                resource_links.append(
                    resource_link_content(
                        str(best_url),
                        str(art.get("filename") or art.get("kind") or "artifact"),
                        description=f"GridForge {art.get('kind')} artifact ({size_kb:.1f} KB)",
                        mime_type="application/pdf" if art.get("kind") == "pdf" else "application/octet-stream",
                    )
                )
        if page_count > 1:
            lines.append(f"- **Pages:** {page_count} PDF pages (use `get_job_page_preview` to inspect secondary sheets/cards).")
        if share_url:
            lines.append(f"- **Studio Share Link:** {share_url}")
        if studio_url and studio_url != share_url:
            lines.append(f"- **Studio Deep Link:** {studio_url}")

        answer_text = "\n".join(lines)
        content.append(text_content(answer_text))
        if preview_png is not None:
            content.append(image_content(preview_png))
        content.extend(resource_links)

        structured = {
            "view": "render",
            "title": title,
            "summary": summary,
            "answer": answer_text,
            "job": job,
            "page_count": page_count,
            "pages": pages,
            "share_url": share_url,
            "studio_url": studio_url,
        }
        return call_tool_result(
            content,
            structured_content=structured,
            is_error=(status_str == "failed"),
        )

    async def _tool_get_job_status(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        job_id = str(args["job_id"]).strip()
        job_raw = await self._api_json(client, "GET", f"/api/jobs/{quote(job_id, safe='')}")
        job = self._enrich_job_urls(job_raw, public_url)
        status_str = str(job.get("status") or "queued")

        page_count = 0
        pages: list[dict[str, Any]] = []
        preview_png: bytes | None = None
        if status_str == "succeeded":
            page_count, pages = await self._fetch_job_pages_safe(client, job_id, job, public_url)
            if bool(args.get("include_preview_image", False)):
                raw_png = await self._fetch_job_page_png_bytes(client, job_id, page=1, width=720)
                if raw_png is not None:
                    preview_png = _fit_png_under_budget(raw_png)

        studio_url = build_studio_url(
            public_url,
            center=job.get("center"),
            scale=int(job.get("scale") or 25000),
            paper=str(job.get("paper") or "a4"),
            orientation=str(job.get("orientation") or "portrait"),
            color_mode=str(job.get("color_mode") or "color"),
            grid_cols=int(job.get("grid_cols") or 1),
            grid_rows=int(job.get("grid_rows") or 1),
            title=job.get("title"),
        )

        title = str(job.get("title") or job.get("center_mgrs") or f"Job {job_id}")
        summary = f"Job `{job_id}`: **{status_str}** ({job.get('stage', 'queued')}, {job.get('progress_pct', 0)}%)."
        lines = [f"**{title}** — {summary}"]
        for art in job.get("artifacts") or []:
            best_url = art.get("signed_download_url") or art.get("download_url")
            lines.append(f"- **{str(art.get('kind', '')).upper()}**: [{art.get('filename')}]({best_url})")
        answer_text = "\n".join(lines)

        content: list[dict[str, Any]] = [text_content(answer_text)]
        if preview_png is not None:
            content.append(image_content(preview_png))

        structured = {
            "view": "render",
            "title": title,
            "summary": summary,
            "answer": answer_text,
            "job": job,
            "page_count": page_count,
            "pages": pages,
            "studio_url": studio_url,
        }
        return call_tool_result(content, structured_content=structured)

    async def _tool_get_job_page_preview(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        _ = public_url
        job_id = str(args["job_id"]).strip()
        page = max(1, int(args.get("page") or 1))
        width = max(240, min(1600, int(args.get("width") or 900)))

        raw_png = await self._fetch_job_page_png_bytes(client, job_id, page=page, width=width)
        if raw_png is None:
            return call_tool_result(
                [text_content(f"Could not fetch page {page} preview for job '{job_id}'.")],
                is_error=True,
            )
        png = _fit_png_under_budget(raw_png)
        meta_text = f"Rendered preview of job `{job_id}` page {page} ({width}px target width, {len(png):,} bytes PNG)."
        return call_tool_result(
            [text_content(meta_text), image_content(png)],
            structured_content={
                "job_id": job_id,
                "page": page,
                "width": width,
                "size_bytes": len(png),
                "mime_type": "image/png",
            },
        )

    async def _tool_create_share_link(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        req_body: dict[str, Any] = {
            "name": str(args.get("name") or ""),
            "notes": str(args.get("notes") or ""),
        }
        if args.get("project_id"):
            req_body["project_id"] = str(args["project_id"])
        if args.get("expires_in_hours") is not None:
            req_body["expires_in_hours"] = int(args["expires_in_hours"])

        state = args.get("state")
        if isinstance(state, dict) and state:
            req_body["state"] = state
        elif not req_body.get("project_id"):
            built_state: dict[str, Any] = {}
            for k in ("center", "scale", "paper", "orientation", "color_mode"):
                if args.get(k) is not None:
                    built_state[k] = args[k]
            if built_state:
                req_body["state"] = built_state

        snap = await self._api_json(client, "POST", "/api/shares", json_body=req_body)
        share_path = str(snap.get("share_path") or f"/?share={snap['share_id']}")
        share_url = _absolutize_url(share_path, public_url) or share_path
        structured = {**snap, "share_url": share_url}
        text = f"Created shareable GridForge Studio link: {share_url} (share_id=`{snap['share_id']}`)"
        return call_tool_result([text_content(text)], structured_content=structured)

    async def _tool_list_packs(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        _ = public_url
        group = str(args.get("group") or "").strip().lower() or None
        packs = await self._fetch_packs_safe(client)
        regions_resp = await self._api_json(client, "GET", "/api/packs/regions")
        regions = list(regions_resp.get("regions") or [])
        if group:
            regions = [r for r in regions if str(r.get("group") or "").lower() == group]

        title = "Offline Regional Packs & Catalog"
        summary = f"{len(packs)} installed pack(s); {len(regions)} catalog region(s)" + (f" in '{group}'." if group else ".")
        lines = [f"**{title}** — {summary}"]
        if packs:
            lines.append("Installed packs: " + ", ".join(f"`{p['pack_id']}` ({p['name']})" for p in packs))
        else:
            lines.append("No offline packs installed yet; live cloud open-data sources remain active.")
        return call_tool_result(
            [text_content("\n".join(lines))],
            structured_content={
                "view": "packs",
                "title": title,
                "summary": summary,
                "packs": packs,
                "regions": regions,
            },
        )

    async def _tool_get_capabilities(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        _ = (args, public_url)
        caps = await self._fetch_capabilities_safe(client)
        scales_str = ", ".join(f"1:{s:,}" for s in (caps.get("scales") or VALID_SCALES))
        text = (
            f"GridForge v{caps.get('version', __version__)} (auth_mode=`{caps.get('auth_mode', 'none')}`)\n"
            f"- Scales: {scales_str}\n"
            f"- Paper sizes: {', '.join(caps.get('paper_sizes') or [])}\n"
            f"- Outputs: {', '.join(caps.get('outputs') or [])}\n"
            f"- Installed offline packs: {caps.get('installed_packs_count', 0)}"
        )
        return call_tool_result([text_content(text)], structured_content=caps)

    # ------------------------------------------------------------------
    # App-Only Tool Handlers (visibility: ["app"])
    # ------------------------------------------------------------------

    async def _tool_app_get_job_status(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        job_id = str(args["job_id"]).strip()
        job_raw = await self._api_json(client, "GET", f"/api/jobs/{quote(job_id, safe='')}")
        job = self._enrich_job_urls(job_raw, public_url)
        page_count, pages = (
            await self._fetch_job_pages_safe(client, job_id, job, public_url)
            if job.get("status") == "succeeded"
            else (0, [])
        )
        return call_tool_result(
            [text_content(f"Job {job_id}: {job.get('status')} ({job.get('progress_pct', 0)}%)")],
            structured_content={
                "job": job,
                "page_count": page_count,
                "pages": pages,
            },
        )

    async def _tool_app_get_job_page(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        job_id = str(args["job_id"]).strip()
        page = max(1, int(args.get("page") or 1))
        width = max(240, min(2400, int(args.get("width") or 1100)))

        raw_png = await self._fetch_job_page_png_bytes(client, job_id, page=page, width=width)
        if raw_png is None:
            return call_tool_result(
                [text_content(f"Page {page} unavailable for job '{job_id}'.")],
                is_error=True,
            )

        page_count = page
        try:
            job_raw = await self._api_json(client, "GET", f"/api/jobs/{quote(job_id, safe='')}")
            page_count, _ = await self._fetch_job_pages_safe(client, job_id, job_raw, public_url)
        except Exception:
            pass

        b64 = base64.b64encode(raw_png).decode("ascii")
        return call_tool_result(
            [text_content(f"Loaded page {page}/{max(1, page_count)} for job {job_id}.")],
            structured_content={
                "job_id": job_id,
                "page": page,
                "page_count": max(page, page_count),
                "mime_type": "image/png",
                "data_base64": b64,
            },
        )

    async def _tool_app_get_capabilities(
        self,
        client: httpx.AsyncClient,
        args: dict[str, Any],
        public_url: str | None,
    ) -> dict[str, Any]:
        _ = (args, public_url)
        caps = await self._fetch_capabilities_safe(client)
        packs = await self._fetch_packs_safe(client)
        return call_tool_result(
            [text_content("Loaded GridForge capabilities and packs.")],
            structured_content={
                "capabilities": caps,
                "packs": packs,
            },
        )
