"""Phase 2 high-detail military cartographic styling for GridForge (Color & B&W).

Provides:
- Multi-scale symbology adapting across 1:15k, 1:24k/1:25k, and 1:48k/1:50k.
- Dedicated Color (classic NATO/USGS topographic) and B&W (monochrome laser-print)
  themes with zero grayscale desaturation.
- High-detail features: highway exits & shields, runways/helipads, power lines/towers,
  pipelines, SVG building/POI usage icons (points & building polygon centroids),
  downstream river flow arrows, separate evergreen/deciduous/mixed tree cover,
  hilltop elevation deduplication, and toggleable ``CartoOptions`` density controls.
- US public-domain regional overlay styling (FEMA NFHL flood zones, NHD hydrography,
  FAA/FCC obstacle towers, NOAA ENC depth contours & navaids).
- Reusable ``populate_project_layers(project, workspace, include_grid=...)`` shared
  by both print PDF rendering and Phase 3 collarless MBTiles/KMZ raster export.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gridforge.config import (
    AU_STATE_TRAIL_OVERLAY_KINDS,
    CAOverlayKind,
    CartoOptions,
    ColorMode,
    DensityLevel,
    HillshadeStrength,
    JobWorkspace,
    NZOverlayKind,
    Units,
    USOverlayKind,
)

logger = logging.getLogger(__name__)

# Ordered roles matching the exact rendering pipeline (and exported native QMLs).
STYLE_ROLES: tuple[str, ...] = (
    "landcover",
    "hillshade",
    "viewshed",
    "slope_shading",
    "fema_flood",
    "au_protected_areas",
    "public_lands",
    "water_polygons",
    "au_hydro_polys",
    "contours",
    "enc_depth_contours",
    "nhd_flowlines",
    "au_hydro_lines",
    "waterways",
    "buildings",
    "transport",
    "au_state_trails",
    "au_power_lines",
    "au_pipelines",
    "au_runways",
    "building_icons",
    "points",
    "au_state_pois",
    "au_substations",
    "au_hydro_points",
    "au_homesteads",
    "au_airfields",
    "enc_navaids",
    "us_towers",
    "mgrs_grid_1km",
    "mgrs_grid_100km",
    "mgrs_zone_boundary",
    "mgrs_square_labels",
    "overlay_polygons",
    "overlay_lines",
    "overlay_points",
    "tactical_sectors",
    "tactical_rings",
    "tactical_lines",
    "waypoint_route",
    "waypoint_points",
    "viewshed_rings",
    "viewshed_observers",
)

DEFAULT_FONT_FAMILY = "Liberation Sans"

_LABELED_ROLES: frozenset[str] = frozenset(
    {
        "fema_flood",
        "au_protected_areas",
        "public_lands",
        "water_polygons",
        "au_hydro_polys",
        "contours",
        "enc_depth_contours",
        "nhd_flowlines",
        "au_hydro_lines",
        "waterways",
        "transport",
        "au_state_trails",
        "au_power_lines",
        "au_pipelines",
        "points",
        "au_state_pois",
        "au_substations",
        "au_hydro_points",
        "au_homesteads",
        "au_airfields",
        "enc_navaids",
        "us_towers",
        "mgrs_square_labels",
        "tactical_sectors",
        "tactical_rings",
        "tactical_lines",
        "waypoint_points",
        "viewshed_observers",
    }
)
_NATIVE_QML_MARKER = "gridforge-pyqgis-exported"
_DEFAULT_CARTO = CartoOptions()

# 4-theme military/topographic palette tables.
# CRITICAL for `night-red`: every critical feature (contours, roads, trails, grid, POIs,
# waypoints, tactical graphics) has low Red-channel reflectance (R <= 61) so nothing
# disappears under a 625-660 nm red headlamp!
_THEME_PALETTE: dict[ColorMode, dict[str, str]] = {
    ColorMode.COLOR: {
        "bg": "#FAF9F5",
        "halo": "#FAF9F5",
        "ink": "#1A1A1A",
        "ink_dark": "#111111",
        "ink_soft": "#2B2B2B",
        "danger": "#C62828",
        "accent_blue": "#1B4F72",
        "accent_blue_dark": "#10324A",
        "contour_idx": "#8F5B28",
        "contour_minor": "#A87846",
        "contour_label": "#78481B",
        "water_fill": "#A6D1EC",
        "water_stroke": "#3A82B5",
        "river": "#2B79B3",
        "stream": "#3D8AC4",
        "water_label": "#1E5B88",
        "nhd_perennial": "#2474B5",
        "nhd_intermit": "#358AC9",
        "nhd_ephem": "#5C9FD4",
        "enc_depth": "#1565C0",
        "motorway_casing": "#5E1D12",
        "motorway_fill": "#C84B31",
        "motorway_center": "#FAF9F5",
        "primary_casing": "#6E3A0C",
        "primary_fill": "#D97925",
        "secondary_casing": "#6E5614",
        "secondary_fill": "#E5B742",
        "tertiary_casing": "#4A4A4A",
        "tertiary_fill": "#FFFFFF",
        "service_casing": "#686868",
        "service_fill": "#F7F7F7",
        "track": "#5E442B",
        "track_rough": "#704A26",
        "trail": "#3D3D3D",
        "cliff": "#4A3B2C",
        "barrier": "#6E6860",
        "rail": "#1A1A1A",
        "runway_fill": "#666666",
        "taxiway_casing": "#333333",
        "taxiway_fill": "#B0B0B0",
        "power": "#555555",
        "au_power": "#4A4A4A",
        "au_sub": "#3D3D3D",
        "pipeline": "#6A4C93",
        "shield_stroke": "#5E1D12",
        "exit_stroke": "#C84B31",
        "exit_text": "#8C1D18",
        "building_fill": "#6E6E6E",
        "building_stroke": "#404040",
        "substation_hatch": "#555555",
        "substation_bg": "#E6E2DC",
        "substation_outline": "#333333",
        "pylon": "#444444",
        "viewpoint": "#333333",
        "grid_1km": "#1B4F72",
        "grid_100km": "#10324A",
        "zone_boundary": "#7B1FA2",
        "square_label": "#1B4F72",
        "overlay_accent": "#C2185B",
        "fema_stroke": "#1565C0",
        "fema_hatch": "#1976D2",
        "fema_bg": "#1E88E5",
        "fema_mod_hatch": "#64B5F6",
        "fema_mod_outline": "#42A5F5",
        "fema_label": "#0D47A1",
        "park_stroke": "#2B7A3E",
        "park_ipa_stroke": "#2E7D6B",
        "park_label": "#1E5C2E",
        "public_np_stroke": "#1F6E43",
        "public_nf_stroke": "#3A8249",
        "public_blm_stroke": "#8C732A",
        "public_label": "#185232",
        "au_bore": "#1B6CA8",
        "au_bore_label": "#145282",
        "au_hydro_major": "#2474B5",
        "au_hydro_minor": "#2E80BF",
        "au_hydro_nonper": "#3D8AC4",
        "au_dry_hatch": "#4B8EC2",
        "au_dry_bg": "#CBE3F2",
        "state_fire_trail": "#D95F02",
        "state_4wd": "#7F4F24",
        "state_walk": "#A63603",
        "state_fire_label": "#A83F00",
        "state_walk_label": "#6B2A0A",
        "poi_hut": "#7A3E1D",
        "poi_camp": "#1E6B3A",
        "poi_lookout": "#1B4F72",
        "poi_hc_label": "#4A230D",
        "wp_route": "#C2185B",
        "wp_camp": "#1E7E34",
        "wp_camp_border": "#0D4D1D",
        "wp_water": "#1565C0",
        "wp_water_border": "#0A3A70",
        "wp_hazard": "#D84315",
        "wp_hazard_border": "#7F2307",
        "wp_lz": "#6A1B9A",
        "wp_lz_border": "#3B0A59",
        "wp_std": "#C2185B",
        "wp_std_border": "#7A0D38",
        "wp_label": "#880E4F",
        "viewshed_ring": "#00897B",
        "viewshed_obs": "#00695C",
        "viewshed_obs_out": "#004D40",
        "tactical_sector": "#7B1FA2",
        "tactical_ring": "#0D47A1",
        "tactical_line": "#B71C1C",
        "tactical_rf_line": "#00695C",
        "tactical_label": "#7F0000",
        "conifer_icon": "#4F7E46",
        "conifer_bg": "#BDD8B0",
        "decid_icon": "#688B48",
        "decid_bg": "#D6E6B8",
        "mixed_bg": "#CFE2C2",
        "scrub_bg": "#E3ECD2",
        "park_lc_bg": "#D8EAD0",
        "park_lc_stroke": "#8FB884",
        "wetland_hatch": "#5A96A8",
        "wetland_bg": "#D2E5E2",
        "rock_bg": "#EAE4D7",
        "urban_bg": "#EFECE5",
        "glacier_bg": "#EBF5FB",
        "glacier_stroke": "#85C1E9",
        "military_hatch": "#C06060",
        "military_bg": "#F4E8E6",
        "military_outline": "#B04A4A",
        "aeroway_bg": "#E2E0DA",
        "aeroway_stroke": "#666666",
    },
    ColorMode.BW: {
        "bg": "#FFFFFF",
        "halo": "#FFFFFF",
        "ink": "#000000",
        "ink_dark": "#000000",
        "ink_soft": "#111111",
        "danger": "#000000",
        "accent_blue": "#000000",
        "accent_blue_dark": "#000000",
        "contour_idx": "#1A1A1A",
        "contour_minor": "#505050",
        "contour_label": "#111111",
        "water_fill": "#FFFFFF",
        "water_stroke": "#1A1A1A",
        "river": "#1A1A1A",
        "stream": "#333333",
        "water_label": "#111111",
        "nhd_perennial": "#1A1A1A",
        "nhd_intermit": "#333333",
        "nhd_ephem": "#555555",
        "enc_depth": "#222222",
        "motorway_casing": "#000000",
        "motorway_fill": "#FFFFFF",
        "motorway_center": "#000000",
        "primary_casing": "#000000",
        "primary_fill": "#C8C8C8",
        "secondary_casing": "#000000",
        "secondary_fill": "#FFFFFF",
        "tertiary_casing": "#222222",
        "tertiary_fill": "#FFFFFF",
        "service_casing": "#444444",
        "service_fill": "#FFFFFF",
        "track": "#1A1A1A",
        "track_rough": "#2A2A2A",
        "trail": "#1A1A1A",
        "cliff": "#1A1A1A",
        "barrier": "#4A4A4A",
        "rail": "#000000",
        "runway_fill": "#444444",
        "taxiway_casing": "#000000",
        "taxiway_fill": "#BDBDBD",
        "power": "#333333",
        "au_power": "#1A1A1A",
        "au_sub": "#000000",
        "pipeline": "#1A1A1A",
        "shield_stroke": "#000000",
        "exit_stroke": "#000000",
        "exit_text": "#000000",
        "building_fill": "#262626",
        "building_stroke": "#000000",
        "substation_hatch": "#222222",
        "substation_bg": "#F2F2F2",
        "substation_outline": "#000000",
        "pylon": "#1A1A1A",
        "viewpoint": "#111111",
        "grid_1km": "#000000",
        "grid_100km": "#000000",
        "zone_boundary": "#000000",
        "square_label": "#1A1A1A",
        "overlay_accent": "#000000",
        "fema_stroke": "#222222",
        "fema_hatch": "#444444",
        "fema_bg": "#FFFFFF",
        "fema_mod_hatch": "#777777",
        "fema_mod_outline": "#555555",
        "fema_label": "#1A1A1A",
        "park_stroke": "#1A1A1A",
        "park_ipa_stroke": "#1A1A1A",
        "park_label": "#1A1A1A",
        "public_np_stroke": "#1A1A1A",
        "public_nf_stroke": "#333333",
        "public_blm_stroke": "#4D4D4D",
        "public_label": "#111111",
        "au_bore": "#000000",
        "au_bore_label": "#000000",
        "au_hydro_major": "#1A1A1A",
        "au_hydro_minor": "#2B2B2B",
        "au_hydro_nonper": "#444444",
        "au_dry_hatch": "#777777",
        "au_dry_bg": "#FFFFFF",
        "state_fire_trail": "#000000",
        "state_4wd": "#1A1A1A",
        "state_walk": "#1A1A1A",
        "state_fire_label": "#000000",
        "state_walk_label": "#1A1A1A",
        "poi_hut": "#000000",
        "poi_camp": "#000000",
        "poi_lookout": "#111111",
        "poi_hc_label": "#000000",
        "wp_route": "#111111",
        "wp_camp": "#111111",
        "wp_camp_border": "#000000",
        "wp_water": "#111111",
        "wp_water_border": "#000000",
        "wp_hazard": "#000000",
        "wp_hazard_border": "#000000",
        "wp_lz": "#000000",
        "wp_lz_border": "#000000",
        "wp_std": "#111111",
        "wp_std_border": "#000000",
        "wp_label": "#000000",
        "viewshed_ring": "#222222",
        "viewshed_obs": "#000000",
        "viewshed_obs_out": "#000000",
        "tactical_sector": "#111111",
        "tactical_ring": "#000000",
        "tactical_line": "#000000",
        "tactical_rf_line": "#222222",
        "tactical_label": "#000000",
    },
    ColorMode.NIGHT_RED: {
        "bg": "#FAF9F7",
        "halo": "#FAF9F7",
        "ink": "#0D1B2A",
        "ink_dark": "#0A141E",
        "ink_soft": "#1E2D3D",
        "danger": "#2B1B69",
        "accent_blue": "#0D3B66",
        "accent_blue_dark": "#092642",
        "contour_idx": "#1B3A4B",
        "contour_minor": "#3C5E73",
        "contour_label": "#0E2636",
        "water_fill": "#96C7E0",
        "water_stroke": "#125282",
        "river": "#104D7A",
        "stream": "#1D669C",
        "water_label": "#0C3C61",
        "nhd_perennial": "#104D7A",
        "nhd_intermit": "#1B6396",
        "nhd_ephem": "#2E78AD",
        "enc_depth": "#0E4B78",
        "motorway_casing": "#0B1622",
        "motorway_fill": "#22538C",
        "motorway_center": "#FFFFFF",
        "primary_casing": "#102030",
        "primary_fill": "#2B7094",
        "secondary_casing": "#1C2C3B",
        "secondary_fill": "#5D9BB8",
        "tertiary_casing": "#22303D",
        "tertiary_fill": "#FFFFFF",
        "service_casing": "#3A4A59",
        "service_fill": "#F5F7F9",
        "track": "#1B3144",
        "track_rough": "#1B3B4B",
        "trail": "#142330",
        "cliff": "#1E2D3D",
        "barrier": "#2B3D4F",
        "rail": "#0A1219",
        "runway_fill": "#3B4A59",
        "taxiway_casing": "#182430",
        "taxiway_fill": "#95A5B3",
        "power": "#2B3D4F",
        "au_power": "#223344",
        "au_sub": "#152330",
        "pipeline": "#36246E",
        "shield_stroke": "#0D2D4F",
        "exit_stroke": "#0D3B66",
        "exit_text": "#0B1F3A",
        "building_fill": "#3D4E5E",
        "building_stroke": "#14202B",
        "substation_hatch": "#2B3D4F",
        "substation_bg": "#E1E7EC",
        "substation_outline": "#14202B",
        "pylon": "#223344",
        "viewpoint": "#182938",
        "grid_1km": "#0D2B45",
        "grid_100km": "#081C2E",
        "zone_boundary": "#261E66",
        "square_label": "#0D2B45",
        "overlay_accent": "#1D2975",
        "fema_stroke": "#0F4C81",
        "fema_hatch": "#165C99",
        "fema_bg": "#1A6EB5",
        "fema_mod_hatch": "#3579B0",
        "fema_mod_outline": "#236499",
        "fema_label": "#092E52",
        "park_stroke": "#145746",
        "park_ipa_stroke": "#144E63",
        "park_label": "#0E3D31",
        "public_np_stroke": "#125245",
        "public_nf_stroke": "#1F5949",
        "public_blm_stroke": "#264A66",
        "public_label": "#0D3B31",
        "au_bore": "#0F4F82",
        "au_bore_label": "#0A3659",
        "au_hydro_major": "#104D7A",
        "au_hydro_minor": "#1A5F91",
        "au_hydro_nonper": "#286FA3",
        "au_dry_hatch": "#266D9E",
        "au_dry_bg": "#C2DFEE",
        "state_fire_trail": "#123E78",
        "state_4wd": "#1B4D5C",
        "state_walk": "#0D4E68",
        "state_fire_label": "#0B2C59",
        "state_walk_label": "#0D3B4F",
        "poi_hut": "#163759",
        "poi_camp": "#145243",
        "poi_lookout": "#0E456B",
        "poi_hc_label": "#0C233B",
        "wp_route": "#1F2470",
        "wp_camp": "#145A46",
        "wp_camp_border": "#0A3327",
        "wp_water": "#0F4C81",
        "wp_water_border": "#082C4D",
        "wp_hazard": "#2E1A6B",
        "wp_hazard_border": "#180C3D",
        "wp_lz": "#10375C",
        "wp_lz_border": "#081E33",
        "wp_std": "#1F2470",
        "wp_std_border": "#0E1242",
        "wp_label": "#111547",
        "viewshed_ring": "#0B5959",
        "viewshed_obs": "#084545",
        "viewshed_obs_out": "#052E2E",
        "tactical_sector": "#25216E",
        "tactical_ring": "#0E4D6B",
        "tactical_line": "#0E2E5E",
        "tactical_rf_line": "#084F4F",
        "tactical_label": "#0A1E3D",
        "conifer_icon": "#2C5944",
        "conifer_bg": "#DFE8E2",
        "decid_icon": "#3A6650",
        "decid_bg": "#E4ECE6",
        "mixed_bg": "#E6EEE8",
        "scrub_bg": "#ECF1ED",
        "park_lc_bg": "#E2EBE4",
        "park_lc_stroke": "#2E5D47",
        "wetland_hatch": "#2B6B7C",
        "wetland_bg": "#DCE9EC",
        "rock_bg": "#ECEEF0",
        "urban_bg": "#EFF1F3",
        "glacier_bg": "#DFECF4",
        "glacier_stroke": "#2B6CB0",
        "military_hatch": "#243A5E",
        "military_bg": "#E3E8F0",
        "military_outline": "#182A47",
        "aeroway_bg": "#E4E8EC",
        "aeroway_stroke": "#2B3D4F",
    },
    ColorMode.ALPINE: {
        "bg": "#F8FBFE",
        "halo": "#F8FBFE",
        "ink": "#14202B",
        "ink_dark": "#0D1821",
        "ink_soft": "#263645",
        "danger": "#B71C1C",
        "accent_blue": "#134E7A",
        "accent_blue_dark": "#0C3352",
        "contour_idx": "#1B5E8A",
        "contour_minor": "#4E89B5",
        "contour_label": "#104266",
        "water_fill": "#99D4F2",
        "water_stroke": "#1A69A4",
        "river": "#145C94",
        "stream": "#2574B3",
        "water_label": "#0E4775",
        "nhd_perennial": "#145C94",
        "nhd_intermit": "#2674B3",
        "nhd_ephem": "#5295C9",
        "enc_depth": "#12569E",
        "motorway_casing": "#4A1C16",
        "motorway_fill": "#B84433",
        "motorway_center": "#F8FBFE",
        "primary_casing": "#5E3310",
        "primary_fill": "#CC7029",
        "secondary_casing": "#5C4B18",
        "secondary_fill": "#D9AE42",
        "tertiary_casing": "#3F4D5A",
        "tertiary_fill": "#FFFFFF",
        "service_casing": "#5D6B78",
        "service_fill": "#F4F7FA",
        "track": "#4A5866",
        "track_rough": "#4E5D6C",
        "trail": "#23313F",
        "cliff": "#2C3E50",
        "barrier": "#52606D",
        "rail": "#14202B",
        "runway_fill": "#5A6775",
        "taxiway_casing": "#2C3A47",
        "taxiway_fill": "#A6B3BF",
        "power": "#4A5568",
        "au_power": "#3E4C59",
        "au_sub": "#2D3748",
        "pipeline": "#5E428A",
        "shield_stroke": "#4A1C16",
        "exit_stroke": "#B84433",
        "exit_text": "#7A1F14",
        "building_fill": "#5A6775",
        "building_stroke": "#2D3748",
        "substation_hatch": "#4A5568",
        "substation_bg": "#E4E9EE",
        "substation_outline": "#2D3748",
        "pylon": "#3E4C59",
        "viewpoint": "#263645",
        "grid_1km": "#133D61",
        "grid_100km": "#0B263D",
        "zone_boundary": "#6A1B9A",
        "square_label": "#133D61",
        "overlay_accent": "#AD1457",
        "fema_stroke": "#1565C0",
        "fema_hatch": "#1976D2",
        "fema_bg": "#1E88E5",
        "fema_mod_hatch": "#64B5F6",
        "fema_mod_outline": "#42A5F5",
        "fema_label": "#0D47A1",
        "park_stroke": "#1D6E52",
        "park_ipa_stroke": "#1E6B73",
        "park_label": "#134E3A",
        "public_np_stroke": "#18664E",
        "public_nf_stroke": "#2D7A56",
        "public_blm_stroke": "#526A80",
        "public_label": "#124C3A",
        "au_bore": "#14629E",
        "au_bore_label": "#0D4570",
        "au_hydro_major": "#145C94",
        "au_hydro_minor": "#216FA8",
        "au_hydro_nonper": "#3782BA",
        "au_dry_hatch": "#3E84B8",
        "au_dry_bg": "#C7E2F4",
        "state_fire_trail": "#C44914",
        "state_4wd": "#4A5D70",
        "state_walk": "#8C2D12",
        "state_fire_label": "#94320A",
        "state_walk_label": "#66210D",
        "poi_hut": "#7A3516",
        "poi_camp": "#1A6648",
        "poi_lookout": "#154E78",
        "poi_hc_label": "#4A1F0C",
        "wp_route": "#AD1457",
        "wp_camp": "#1B7340",
        "wp_camp_border": "#0C4223",
        "wp_water": "#135EA8",
        "wp_water_border": "#093663",
        "wp_hazard": "#C62828",
        "wp_hazard_border": "#731313",
        "wp_lz": "#6A1B9A",
        "wp_lz_border": "#380B54",
        "wp_std": "#AD1457",
        "wp_std_border": "#6B0934",
        "wp_label": "#7A0B3B",
        "viewshed_ring": "#00796B",
        "viewshed_obs": "#005B4F",
        "viewshed_obs_out": "#003D33",
        "tactical_sector": "#6A1B9A",
        "tactical_ring": "#1565C0",
        "tactical_line": "#AD1457",
        "tactical_rf_line": "#00695C",
        "tactical_label": "#7A0B3B",
        "conifer_icon": "#356B52",
        "conifer_bg": "#C5DFD0",
        "decid_icon": "#4B7A58",
        "decid_bg": "#D6E7D4",
        "mixed_bg": "#D2E5D8",
        "scrub_bg": "#E4EFE8",
        "park_lc_bg": "#D8ECE2",
        "park_lc_stroke": "#4D8A6E",
        "wetland_hatch": "#3E879E",
        "wetland_bg": "#D0E7EC",
        "rock_bg": "#DCE2E8",
        "urban_bg": "#EAEFF4",
        "glacier_bg": "#E2F2FC",
        "glacier_stroke": "#6BAED6",
        "military_hatch": "#A64B4B",
        "military_bg": "#F2E8EA",
        "military_outline": "#8C3B3B",
        "aeroway_bg": "#DFE5EB",
        "aeroway_stroke": "#4A5866",
    },
}


def _pal(color_mode: ColorMode, key: str) -> str:
    """Look up a cartographic theme color hex for ``color_mode``."""
    return _THEME_PALETTE.get(color_mode, _THEME_PALETTE[ColorMode.COLOR])[key]


def _is_bw(color_mode: ColorMode) -> bool:
    """Return True when ``color_mode`` uses monochrome laser-print hatching/stippling."""
    return color_mode == ColorMode.BW


def get_styles_root() -> Path:
    """Locate ``assets/styles`` relative to the repository or installed package."""
    candidates = [
        Path(__file__).resolve().parents[3] / "assets" / "styles",
        Path.cwd() / "assets" / "styles",
        Path("/app/assets/styles"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def get_icons_root() -> Path:
    """Locate ``assets/icons`` relative to the repository or installed package."""
    candidates = [
        Path(__file__).resolve().parents[3] / "assets" / "icons",
        Path.cwd() / "assets" / "icons",
        Path("/app/assets/icons"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def get_icon_path(name: str, icons_root: Path | None = None) -> Path:
    """Return absolute path to a bundled military/topo SVG icon."""
    root = icons_root if icons_root is not None else get_icons_root()
    fname = name if name.endswith(".svg") else f"{name}.svg"
    return root / fname


def get_qml_path(role: str, color_mode: ColorMode, styles_root: Path | None = None) -> Path:
    """Return the versioned ``.qml`` path for a layer role and theme."""
    root = styles_root if styles_root is not None else get_styles_root()
    return root / color_mode.value / f"{role}.qml"


def map_background_hex(color_mode: ColorMode) -> str:
    """Return the map frame canvas background color hex for the chosen theme."""
    return _pal(color_mode, "bg")


def scale_width_factor(scale: int) -> float:
    """Return cartographic line/symbol width multiplier across supported scales."""
    if scale <= 12500:
        return 1.24
    if scale <= 15000:
        return 1.15
    if scale >= 48000:
        return 0.82
    return 1.0


def scale_font_delta(scale: int) -> float:
    """Return subtle point-size delta for multi-scale typography balance."""
    if scale <= 12500:
        return 0.6
    if scale <= 15000:
        return 0.4
    if scale >= 48000:
        return -0.5
    return 0.0


def _density_params(label_density: DensityLevel) -> tuple[float, float]:
    """Return ``(repeat_multiplier, min_feature_multiplier)`` for PAL labeling."""
    if label_density == DensityLevel.LOW:
        return (1.45, 1.50)
    if label_density == DensityLevel.HIGH:
        return (0.68, 0.58)
    return (1.0, 1.0)


def _hillshade_params(
    color_mode: ColorMode,
    strength: HillshadeStrength,
) -> tuple[float, int, int]:
    """Return ``(opacity, brightness, contrast)`` for hillshade Multiply blend."""
    if color_mode == ColorMode.COLOR:
        table = {
            HillshadeStrength.LIGHT: (0.18, 12, 2),
            HillshadeStrength.NORMAL: (0.32, 8, 5),
            HillshadeStrength.STRONG: (0.48, 0, 10),
        }
    elif color_mode == ColorMode.ALPINE:
        table = {
            HillshadeStrength.LIGHT: (0.22, 10, 4),
            HillshadeStrength.NORMAL: (0.36, 4, 8),
            HillshadeStrength.STRONG: (0.52, -4, 14),
        }
    elif color_mode == ColorMode.NIGHT_RED:
        table = {
            HillshadeStrength.LIGHT: (0.14, 24, 0),
            HillshadeStrength.NORMAL: (0.24, 18, 2),
            HillshadeStrength.STRONG: (0.36, 10, 6),
        }
    else:
        table = {
            HillshadeStrength.LIGHT: (0.12, 46, -8),
            HillshadeStrength.NORMAL: (0.20, 38, -6),
            HillshadeStrength.STRONG: (0.30, 26, 4),
        }
    return table.get(strength, table[HillshadeStrength.NORMAL])


def _make_text_format(
    size_pt: float,
    color_hex: str,
    *,
    bold: bool = False,
    italic: bool = False,
    halo_hex: str = "#FFFFFF",
    halo_mm: float = 0.65,
    halo_opacity: float = 0.92,
    opacity: float = 1.0,
    font_family: str = DEFAULT_FONT_FAMILY,
    badge_fill_hex: str | None = None,
    badge_stroke_hex: str | None = None,
) -> Any:
    """Construct a PyQGIS ``QgsTextFormat`` with crisp vector typography and optional shield badge."""
    from qgis.core import (
        QgsTextBackgroundSettings,
        QgsTextBufferSettings,
        QgsTextFormat,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import QSizeF
    from qgis.PyQt.QtGui import QColor, QFont

    font = QFont(font_family)
    font.setBold(bold)
    font.setItalic(italic)

    fmt = QgsTextFormat()
    fmt.setFont(font)
    fmt.setSize(max(4.5, size_pt))
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    fmt.setColor(QColor(color_hex))
    fmt.setOpacity(opacity)

    if halo_mm > 0 and badge_fill_hex is None:
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(halo_mm)
        buf.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        buf.setColor(QColor(halo_hex))
        buf.setOpacity(halo_opacity)
        fmt.setBuffer(buf)

    if badge_fill_hex is not None:
        bg = QgsTextBackgroundSettings()
        bg.setEnabled(True)
        bg.setType(QgsTextBackgroundSettings.ShapeRectangle)
        bg.setSizeType(QgsTextBackgroundSettings.SizeBuffer)
        bg.setSize(QSizeF(0.65, 0.35))
        bg.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        bg.setRadii(QSizeF(0.45, 0.45))
        bg.setRadiiUnit(QgsUnitTypes.RenderMillimeters)
        bg.setFillColor(QColor(badge_fill_hex))
        if badge_stroke_hex is not None:
            bg.setStrokeColor(QColor(badge_stroke_hex))
            bg.setStrokeWidth(0.22)
            bg.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
        fmt.setBackground(bg)

    return fmt


def _svg_marker_symbol(
    icon_name: str,
    size_mm: float,
    *,
    fill_hex: str = "#1A1A1A",
    outline_hex: str = "#1A1A1A",
    icons_root: Path | None = None,
) -> Any:
    """Create a crisp parameterized ``QgsMarkerSymbol`` backed by ``assets/icons/<icon>.svg``."""
    from qgis.core import QgsMarkerSymbol, QgsSvgMarkerSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtGui import QColor

    svg_path = get_icon_path(icon_name, icons_root=icons_root)
    if not svg_path.is_file():
        return QgsMarkerSymbol.createSimple(
            {
                "name": "circle",
                "color": fill_hex,
                "outline_color": "#FFFFFF",
                "outline_width": "0.25",
                "outline_width_unit": "MM",
                "size": f"{size_mm:.3f}",
                "size_unit": "MM",
            }
        )

    sym = QgsMarkerSymbol()
    sym.deleteSymbolLayer(0)
    svg_layer = QgsSvgMarkerSymbolLayer(str(svg_path), size_mm)
    svg_layer.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    svg_layer.setFillColor(QColor(fill_hex))
    svg_layer.setStrokeColor(QColor(outline_hex))
    sym.appendSymbolLayer(svg_layer)
    return sym


def _centroid_svg_fill_symbol(
    icon_name: str,
    size_mm: float,
    *,
    fill_hex: str = "#1A1A1A",
    outline_hex: str = "#1A1A1A",
    icons_root: Path | None = None,
) -> Any:
    """Place a crisp SVG usage icon at the interior centroid of a building polygon."""
    from qgis.core import QgsCentroidFillSymbolLayer, QgsFillSymbol

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)
    centroid = QgsCentroidFillSymbolLayer()
    centroid.setPointOnSurface(True)
    centroid.setSubSymbol(
        _svg_marker_symbol(
            icon_name,
            size_mm,
            fill_hex=fill_hex,
            outline_hex=outline_hex,
            icons_root=icons_root,
        )
    )
    sym.appendSymbolLayer(centroid)
    return sym


def _simple_fill_symbol(
    fill_hex: str,
    stroke_hex: str = "#000000",
    stroke_mm: float = 0.0,
    *,
    stroke_style: str = "no",
    fill_opacity: float = 1.0,
) -> Any:
    from qgis.core import QgsFillSymbol

    is_transparent = fill_hex == "transparent"
    props: dict[str, str] = {
        "color": "0,0,0,0" if is_transparent else fill_hex,
        "style": "no" if is_transparent else "solid",
        "outline_color": stroke_hex,
        "outline_style": stroke_style if stroke_mm > 0 else "no",
        "outline_width": f"{stroke_mm:.3f}",
        "outline_width_unit": "MM",
    }
    sym = QgsFillSymbol.createSimple(props)
    if fill_opacity < 1.0:
        sym.setOpacity(fill_opacity)
    return sym


def _hatch_fill_symbol(
    line_hex: str,
    angle_deg: float,
    spacing_mm: float,
    line_mm: float = 0.12,
    *,
    bg_hex: str | None = None,
    bg_alpha: int = 255,
    outline_hex: str | None = None,
    outline_mm: float = 0.0,
    outline_dash: list[float] | None = None,
    dash_vector: list[float] | None = None,
) -> Any:
    """Build a crisp vector line-pattern hatch fill (ideal for B&W laser prints & FEMA)."""
    from qgis.core import (
        QgsFillSymbol,
        QgsLinePatternFillSymbolLayer,
        QgsLineSymbol,
        QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)

    if bg_hex is not None:
        c = QColor(bg_hex)
        c.setAlpha(bg_alpha)
        bg = QgsSimpleFillSymbolLayer(c)
        bg.setStrokeStyle(Qt.NoPen)
        sym.appendSymbolLayer(bg)

    pattern = QgsLinePatternFillSymbolLayer()
    pattern.setLineAngle(angle_deg)
    pattern.setDistance(spacing_mm)
    pattern.setDistanceUnit(QgsUnitTypes.RenderMillimeters)

    sub_line = QgsLineSymbol()
    sub_line.deleteSymbolLayer(0)
    sl = QgsSimpleLineSymbolLayer(QColor(line_hex), line_mm)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    if dash_vector:
        sl.setUseCustomDashPattern(True)
        sl.setCustomDashVector(dash_vector)
        sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sub_line.appendSymbolLayer(sl)
    pattern.setSubSymbol(sub_line)
    sym.appendSymbolLayer(pattern)

    if outline_hex is not None and outline_mm > 0:
        if outline_dash:
            ol = QgsSimpleLineSymbolLayer(QColor(outline_hex), outline_mm)
            ol.setWidthUnit(QgsUnitTypes.RenderMillimeters)
            ol.setUseCustomDashPattern(True)
            ol.setCustomDashVector(outline_dash)
            ol.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
            sym.appendSymbolLayer(ol)
        else:
            outline = QgsSimpleFillSymbolLayer()
            outline.setBrushStyle(Qt.NoBrush)
            outline.setStrokeColor(QColor(outline_hex))
            outline.setStrokeWidth(outline_mm)
            outline.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
            sym.appendSymbolLayer(outline)

    return sym


def _svg_pattern_fill_symbol(
    icon_name: str,
    *,
    spacing_mm: float,
    icon_mm: float,
    icon_hex: str,
    bg_hex: str | None = None,
    hatch_hex: str | None = None,
    hatch_angle_deg: float = 60.0,
    hatch_spacing_mm: float = 2.8,
    hatch_mm: float = 0.10,
    icons_root: Path | None = None,
) -> Any:
    """Build a canopy pattern fill combining optional soft background/hatch + SVG tree icons."""
    from qgis.core import (
        QgsFillSymbol,
        QgsLinePatternFillSymbolLayer,
        QgsLineSymbol,
        QgsPointPatternFillSymbolLayer,
        QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)

    if bg_hex is not None:
        bg = QgsSimpleFillSymbolLayer(QColor(bg_hex))
        bg.setStrokeStyle(Qt.NoPen)
        sym.appendSymbolLayer(bg)

    if hatch_hex is not None:
        hpat = QgsLinePatternFillSymbolLayer()
        hpat.setLineAngle(hatch_angle_deg)
        hpat.setDistance(hatch_spacing_mm)
        hpat.setDistanceUnit(QgsUnitTypes.RenderMillimeters)
        sub_line = QgsLineSymbol()
        sub_line.deleteSymbolLayer(0)
        sl = QgsSimpleLineSymbolLayer(QColor(hatch_hex), hatch_mm)
        sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        sub_line.appendSymbolLayer(sl)
        hpat.setSubSymbol(sub_line)
        sym.appendSymbolLayer(hpat)

    pattern = QgsPointPatternFillSymbolLayer()
    pattern.setDistanceX(spacing_mm)
    pattern.setDistanceY(spacing_mm)
    pattern.setDistanceXUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDistanceYUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDisplacementX(spacing_mm * 0.5)
    pattern.setDisplacementXUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setSubSymbol(
        _svg_marker_symbol(
            icon_name,
            icon_mm,
            fill_hex=icon_hex,
            outline_hex=icon_hex,
            icons_root=icons_root,
        )
    )
    sym.appendSymbolLayer(pattern)
    return sym


def _stipple_fill_symbol(
    dot_hex: str,
    spacing_mm: float = 2.8,
    dot_mm: float = 0.28,
    *,
    bg_hex: str | None = None,
    outline_hex: str | None = None,
    outline_mm: float = 0.0,
) -> Any:
    """Build a subtle vector dot-stipple fill for scrub/sand in B&W mode."""
    from qgis.core import (
        QgsFillSymbol,
        QgsMarkerSymbol,
        QgsPointPatternFillSymbolLayer,
        QgsSimpleFillSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)

    if bg_hex is not None:
        bg = QgsSimpleFillSymbolLayer(QColor(bg_hex))
        bg.setStrokeStyle(Qt.NoPen)
        sym.appendSymbolLayer(bg)

    pattern = QgsPointPatternFillSymbolLayer()
    pattern.setDistanceX(spacing_mm)
    pattern.setDistanceY(spacing_mm)
    pattern.setDistanceXUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDistanceYUnit(QgsUnitTypes.RenderMillimeters)
    pattern.setDisplacementX(spacing_mm * 0.5)
    pattern.setDisplacementXUnit(QgsUnitTypes.RenderMillimeters)

    marker = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": dot_hex,
            "outline_style": "no",
            "size": f"{dot_mm:.3f}",
            "size_unit": "MM",
        }
    )
    pattern.setSubSymbol(marker)
    sym.appendSymbolLayer(pattern)

    if outline_hex is not None and outline_mm > 0:
        outline = QgsSimpleFillSymbolLayer()
        outline.setBrushStyle(Qt.NoBrush)
        outline.setStrokeColor(QColor(outline_hex))
        outline.setStrokeWidth(outline_mm)
        outline.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
        sym.appendSymbolLayer(outline)

    return sym


def _line_symbol(
    color_hex: str,
    width_mm: float,
    *,
    dash_vector: list[float] | None = None,
    cap_round: bool = True,
    render_pass: int = 0,
) -> Any:
    from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)
    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), width_mm)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.RoundCap if cap_round else Qt.FlatCap)
    sl.setPenJoinStyle(Qt.RoundJoin)
    if dash_vector:
        sl.setUseCustomDashPattern(True)
        sl.setCustomDashVector(dash_vector)
        sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sl.setRenderingPass(render_pass)
    sym.appendSymbolLayer(sl)
    return sym


def _waterway_symbol(
    color_hex: str,
    width_mm: float,
    *,
    dash_vector: list[float] | None = None,
    show_arrows: bool = False,
    arrow_interval_mm: float = 30.0,
    arrow_size_mm: float = 1.9,
    arrow_color_hex: str | None = None,
    icons_root: Path | None = None,
) -> Any:
    """Waterway line symbol with optional downstream directional chevron arrows."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), width_mm)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.RoundCap)
    sl.setPenJoinStyle(Qt.RoundJoin)
    if dash_vector:
        sl.setUseCustomDashPattern(True)
        sl.setCustomDashVector(dash_vector)
        sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sym.appendSymbolLayer(sl)

    if show_arrows:
        arrows = QgsMarkerLineSymbolLayer(True, arrow_interval_mm)
        arrows.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
        if hasattr(arrows, "setOffsetAlongLine"):
            arrows.setOffsetAlongLine(arrow_interval_mm * 0.45)
            arrows.setOffsetAlongLineUnit(QgsUnitTypes.RenderMillimeters)
        ac = arrow_color_hex or color_hex
        arrows.setSubSymbol(
            _svg_marker_symbol(
                "river_arrow",
                arrow_size_mm,
                fill_hex=ac,
                outline_hex=ac,
                icons_root=icons_root,
            )
        )
        sym.appendSymbolLayer(arrows)

    return sym


def _cased_road_symbol(
    casing_hex: str,
    casing_mm: float,
    fill_hex: str,
    fill_mm: float,
    *,
    casing_pass: int,
    fill_pass: int,
    centerline_hex: str | None = None,
    centerline_mm: float = 0.0,
    centerline_dash: list[float] | None = None,
    centerline_pass: int = 28,
) -> Any:
    """Create a multi-layer road/runway symbol with explicit symbol-level passes."""
    from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    casing = QgsSimpleLineSymbolLayer(QColor(casing_hex), casing_mm)
    casing.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    casing.setPenCapStyle(Qt.RoundCap)
    casing.setPenJoinStyle(Qt.RoundJoin)
    casing.setRenderingPass(casing_pass)
    sym.appendSymbolLayer(casing)

    fill = QgsSimpleLineSymbolLayer(QColor(fill_hex), fill_mm)
    fill.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    fill.setPenCapStyle(Qt.RoundCap)
    fill.setPenJoinStyle(Qt.RoundJoin)
    fill.setRenderingPass(fill_pass)
    sym.appendSymbolLayer(fill)

    if centerline_hex is not None and centerline_mm > 0:
        center = QgsSimpleLineSymbolLayer(QColor(centerline_hex), centerline_mm)
        center.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        center.setPenCapStyle(Qt.FlatCap)
        center.setPenJoinStyle(Qt.RoundJoin)
        if centerline_dash:
            center.setUseCustomDashPattern(True)
            center.setCustomDashVector(centerline_dash)
            center.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
        center.setRenderingPass(centerline_pass)
        sym.appendSymbolLayer(center)

    return sym


def _railway_symbol(color_hex: str = "#1A1A1A", base_pass: int = 15, wf: float = 1.0) -> Any:
    """Create classic topographic railway line with perpendicular cross-hatch ticks."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsMarkerSymbol,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    rail = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.30 * wf)
    rail.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    rail.setPenCapStyle(Qt.FlatCap)
    rail.setPenJoinStyle(Qt.RoundJoin)
    rail.setRenderingPass(base_pass)
    sym.appendSymbolLayer(rail)

    ticks = QgsMarkerLineSymbolLayer(True, 3.2)
    ticks.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    ticks.setRenderingPass(base_pass + 1)

    tick_marker = QgsMarkerSymbol.createSimple(
        {
            "name": "line",
            "color": color_hex,
            "outline_color": color_hex,
            "outline_width": f"{0.24 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{1.35 * wf:.3f}",
            "size_unit": "MM",
            "angle": "0",
        }
    )
    ticks.setSubSymbol(tick_marker)
    sym.appendSymbolLayer(ticks)
    return sym


def _cliff_symbol(color_hex: str, render_pass: int = 4, wf: float = 1.0) -> Any:
    """Create USGS/NATO topographic cliff escarpment line with downslope hachure ticks."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsMarkerSymbol,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    edge = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.24 * wf)
    edge.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    edge.setPenCapStyle(Qt.FlatCap)
    edge.setPenJoinStyle(Qt.RoundJoin)
    edge.setRenderingPass(render_pass)
    sym.appendSymbolLayer(edge)

    hachures = QgsMarkerLineSymbolLayer(True, 1.8 * wf)
    hachures.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    hachures.setRenderingPass(render_pass)

    tick_marker = QgsMarkerSymbol.createSimple(
        {
            "name": "line",
            "color": color_hex,
            "outline_color": color_hex,
            "outline_width": f"{0.20 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{1.05 * wf:.3f}",
            "size_unit": "MM",
            "angle": "0",
            "offset": f"0,{0.48 * wf:.3f}",
            "offset_unit": "MM",
        }
    )
    hachures.setSubSymbol(tick_marker)
    sym.appendSymbolLayer(hachures)
    return sym


def _powerline_symbol(color_hex: str, render_pass: int = 2, wf: float = 1.0) -> Any:
    """Military topo power transmission line: crisp thin line + perpendicular pylon ticks."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsMarkerSymbol,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.16 * wf)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.FlatCap)
    sl.setRenderingPass(render_pass)
    sym.appendSymbolLayer(sl)

    pylons = QgsMarkerLineSymbolLayer(True, 9.5)
    pylons.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    pylons.setRenderingPass(render_pass)
    pylon_marker = QgsMarkerSymbol.createSimple(
        {
            "name": "line",
            "color": color_hex,
            "outline_color": color_hex,
            "outline_width": f"{0.22 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{1.15 * wf:.3f}",
            "size_unit": "MM",
            "angle": "0",
        }
    )
    pylons.setSubSymbol(pylon_marker)
    sym.appendSymbolLayer(pylons)
    return sym


def _pipeline_symbol(color_hex: str, render_pass: int = 2, wf: float = 1.0) -> Any:
    """Distinct topo pipeline symbol: dash-dot line with hollow circle valve markers."""
    from qgis.core import (
        QgsLineSymbol,
        QgsMarkerLineSymbolLayer,
        QgsMarkerSymbol,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    sl = QgsSimpleLineSymbolLayer(QColor(color_hex), 0.22 * wf)
    sl.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    sl.setPenCapStyle(Qt.FlatCap)
    sl.setUseCustomDashPattern(True)
    sl.setCustomDashVector([3.0, 1.2])
    sl.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sl.setRenderingPass(render_pass)
    sym.appendSymbolLayer(sl)

    circles = QgsMarkerLineSymbolLayer(False, 8.4)
    circles.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    circles.setRenderingPass(render_pass)
    ring = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": "#FFFFFF",
            "outline_color": color_hex,
            "outline_width": f"{0.22 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{0.95 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    circles.setSubSymbol(ring)
    sym.appendSymbolLayer(circles)
    return sym


def _add_rule(
    root_rule: Any,
    label: str,
    filter_expr: str,
    symbol: Any,
) -> Any:
    from qgis.core import QgsRuleBasedRenderer

    rule = QgsRuleBasedRenderer.Rule(symbol)
    rule.setLabel(label)
    rule.setFilterExpression(filter_expr)
    root_rule.appendChild(rule)
    return rule


def _configure_curved_line_pal(
    pal: Any,
    *,
    on_line: bool = True,
    repeat_mm: float = 0.0,
    min_feature_mm: float = 12.0,
) -> None:
    """Configure curved line PAL placement safely across QGIS 3.34-3.40 APIs."""
    from qgis.core import Qgis, QgsPalLayerSettings, QgsUnitTypes

    try:
        pal.placement = Qgis.LabelPlacement.Curved
    except AttributeError:
        pal.placement = QgsPalLayerSettings.Curved

    if hasattr(pal, "lineSettings"):
        ls = pal.lineSettings()
        try:
            flag = (
                Qgis.LabelLinePlacementFlag.OnLine
                if on_line
                else Qgis.LabelLinePlacementFlag.AboveLine
            )
            ls.setPlacementFlags(flag)
        except (AttributeError, TypeError):
            try:
                pal.placementFlags = (
                    QgsPalLayerSettings.OnLine if on_line else QgsPalLayerSettings.AboveLine
                )
            except AttributeError:
                pass
        if hasattr(ls, "setMergeLines"):
            ls.setMergeLines(True)
        if hasattr(pal, "setLineSettings"):
            pal.setLineSettings(ls)
    else:
        pal.placementFlags = (
            QgsPalLayerSettings.OnLine if on_line else QgsPalLayerSettings.AboveLine
        )

    if hasattr(pal, "mergeLines"):
        pal.mergeLines = True

    if repeat_mm > 0:
        pal.repeatDistance = repeat_mm
        pal.repeatDistanceUnit = QgsUnitTypes.RenderMillimeters

    if hasattr(pal, "thinningSettings"):
        ts = pal.thinningSettings()
        if hasattr(ts, "setMinimumFeatureSize"):
            ts.setMinimumFeatureSize(min_feature_mm)
        if hasattr(pal, "setThinningSettings"):
            pal.setThinningSettings(ts)
    if hasattr(pal, "minimumFeatureSize"):
        try:
            pal.minimumFeatureSize = min_feature_mm
        except AttributeError:
            pass


def _configure_point_pal(pal: Any, *, over_point: bool = False, dist_mm: float = 1.4) -> None:
    from qgis.core import Qgis, QgsPalLayerSettings, QgsUnitTypes

    if over_point:
        try:
            pal.placement = Qgis.LabelPlacement.OverPoint
        except AttributeError:
            pal.placement = QgsPalLayerSettings.OverPoint
        pal.dist = 0.0
    else:
        try:
            pal.placement = Qgis.LabelPlacement.AroundPoint
        except AttributeError:
            pal.placement = QgsPalLayerSettings.AroundPoint
        pal.dist = dist_mm
        pal.distUnits = QgsUnitTypes.RenderMillimeters


def _build_landcover_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    from qgis.core import QgsRuleBasedRenderer

    root = QgsRuleBasedRenderer.Rule(None)
    bw = _is_bw(color_mode)

    if carto.show_trees:
        evergreen_expr = (
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND (\"leaf_type\" = 'needleleaved' OR \"wood\" = 'coniferous' "
            "OR \"leaf_cycle\" = 'evergreen')"
        )
        deciduous_expr = (
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND (\"leaf_type\" = 'broadleaved' OR \"wood\" = 'deciduous' "
            "OR \"leaf_cycle\" = 'deciduous') "
            "AND NOT (\"leaf_type\" = 'needleleaved' OR \"wood\" = 'coniferous')"
        )
        mixed_forest_expr = (
            "(\"landuse\" IN ('forest','wood') OR \"natural\" IN ('wood','forest')) "
            "AND NOT (\"leaf_type\" IN ('needleleaved','broadleaved') "
            "OR \"wood\" IN ('coniferous','deciduous') "
            "OR \"leaf_cycle\" IN ('evergreen','deciduous'))"
        )

        if not bw:
            _add_rule(
                root,
                "Evergreen / Coniferous Forest",
                evergreen_expr,
                _svg_pattern_fill_symbol(
                    "conifer",
                    spacing_mm=6.2,
                    icon_mm=1.6,
                    icon_hex=_pal(color_mode, "conifer_icon"),
                    bg_hex=_pal(color_mode, "conifer_bg"),
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Deciduous / Broadleaf Forest",
                deciduous_expr,
                _svg_pattern_fill_symbol(
                    "deciduous",
                    spacing_mm=6.2,
                    icon_mm=1.6,
                    icon_hex=_pal(color_mode, "decid_icon"),
                    bg_hex=_pal(color_mode, "decid_bg"),
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Mixed / Unspecified Forest",
                mixed_forest_expr,
                _simple_fill_symbol(_pal(color_mode, "mixed_bg")),
            )
            _add_rule(
                root,
                "Scrub / Heath / Grassland",
                "\"natural\" IN ('scrub','heath','grassland','fell') "
                "OR \"landuse\" IN ('meadow','grass','orchard','vineyard','farmland')",
                _simple_fill_symbol(_pal(color_mode, "scrub_bg")),
            )
            _add_rule(
                root,
                "Parks & Reserves",
                "\"leisure\" IN ('park','nature_reserve','golf_course','recreation_ground') "
                "OR \"landuse\" = 'recreation_ground'",
                _simple_fill_symbol(
                    _pal(color_mode, "park_lc_bg"),
                    stroke_hex=_pal(color_mode, "park_lc_stroke"),
                    stroke_mm=0.14,
                    stroke_style="dash",
                ),
            )
        else:
            # Dedicated B&W laser-print canopy patterns: distinct evergreen vs deciduous vs mixed
            _add_rule(
                root,
                "Evergreen / Coniferous Forest",
                evergreen_expr,
                _svg_pattern_fill_symbol(
                    "conifer",
                    spacing_mm=5.6,
                    icon_mm=1.65,
                    icon_hex="#5E5E5E",
                    hatch_hex="#B0B0B0",
                    hatch_angle_deg=60.0,
                    hatch_spacing_mm=2.6,
                    hatch_mm=0.11,
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Deciduous / Broadleaf Forest",
                deciduous_expr,
                _svg_pattern_fill_symbol(
                    "deciduous",
                    spacing_mm=5.4,
                    icon_mm=1.65,
                    icon_hex="#5E5E5E",
                    icons_root=icons_root,
                ),
            )
            _add_rule(
                root,
                "Mixed / Unspecified Forest",
                mixed_forest_expr,
                _hatch_fill_symbol("#969696", angle_deg=45.0, spacing_mm=2.0, line_mm=0.14),
            )
            _add_rule(
                root,
                "Scrub / Heath / Grassland",
                "\"natural\" IN ('scrub','heath','grassland','fell') "
                "OR \"landuse\" IN ('meadow','grass','orchard','vineyard')",
                _stipple_fill_symbol("#8C8C8C", spacing_mm=3.0, dot_mm=0.30),
            )
            _add_rule(
                root,
                "Parks & Reserves",
                "\"leisure\" IN ('park','nature_reserve','golf_course','recreation_ground') "
                "OR \"landuse\" = 'recreation_ground'",
                _simple_fill_symbol("transparent", stroke_hex="#666666", stroke_mm=0.18, stroke_style="dash"),
            )

    if not bw:
        _add_rule(
            root,
            "Glaciers & Permanent Snow",
            "\"natural\" IN ('glacier','snow')",
            _simple_fill_symbol(
                _pal(color_mode, "glacier_bg"),
                stroke_hex=_pal(color_mode, "glacier_stroke"),
                stroke_mm=0.18 if color_mode == ColorMode.ALPINE else 0.14,
                stroke_style="dash" if color_mode == ColorMode.NIGHT_RED else "solid",
            ),
        )
        _add_rule(
            root,
            "Wetland / Marsh",
            "\"natural\" IN ('wetland','mud')",
            _hatch_fill_symbol(
                _pal(color_mode, "wetland_hatch"),
                angle_deg=0.0,
                spacing_mm=2.2,
                line_mm=0.14,
                bg_hex=_pal(color_mode, "wetland_bg"),
                dash_vector=[1.8, 1.4],
            ),
        )
        _add_rule(
            root,
            "Bare Rock / Sand / Scree",
            "\"natural\" IN ('bare_rock','scree','shingle','sand','beach')",
            _simple_fill_symbol(_pal(color_mode, "rock_bg")),
        )
        _add_rule(
            root,
            "Built-up Residential / Commercial",
            "\"landuse\" IN ('residential','commercial','industrial','retail')",
            _simple_fill_symbol(_pal(color_mode, "urban_bg")),
        )
        _add_rule(
            root,
            "Military Area",
            "\"landuse\" = 'military'",
            _hatch_fill_symbol(
                _pal(color_mode, "military_hatch"),
                angle_deg=45.0,
                spacing_mm=3.0,
                line_mm=0.14,
                bg_hex=_pal(color_mode, "military_bg"),
                outline_hex=_pal(color_mode, "military_outline"),
                outline_mm=0.22,
            ),
        )
        _add_rule(
            root,
            "Aeroway Area",
            "\"aeroway\" IN ('aerodrome','runway','apron','taxiway')",
            _simple_fill_symbol(
                _pal(color_mode, "aeroway_bg"),
                stroke_hex=_pal(color_mode, "aeroway_stroke"),
                stroke_mm=0.18,
                stroke_style="solid",
            ),
        )
    else:
        _add_rule(
            root,
            "Glaciers & Permanent Snow",
            "\"natural\" IN ('glacier','snow')",
            _stipple_fill_symbol("#888888", spacing_mm=3.2, dot_mm=0.22, outline_hex="#444444", outline_mm=0.16),
        )
        _add_rule(
            root,
            "Wetland / Marsh",
            "\"natural\" IN ('wetland','mud')",
            _hatch_fill_symbol("#7A7A7A", angle_deg=0.0, spacing_mm=2.2, line_mm=0.14, dash_vector=[1.8, 1.4]),
        )
        _add_rule(
            root,
            "Bare Rock / Sand",
            "\"natural\" IN ('bare_rock','scree','shingle','sand','beach')",
            _stipple_fill_symbol("#999999", spacing_mm=2.4, dot_mm=0.24),
        )
        _add_rule(
            root,
            "Military Area",
            "\"landuse\" = 'military'",
            _hatch_fill_symbol(
                "#555555",
                angle_deg=135.0,
                spacing_mm=3.2,
                line_mm=0.16,
                outline_hex="#222222",
                outline_mm=0.26,
            ),
        )
        _add_rule(
            root,
            "Aeroway Area",
            "\"aeroway\" IN ('aerodrome','runway','apron','taxiway')",
            _simple_fill_symbol("#ECECEC", stroke_hex="#222222", stroke_mm=0.22, stroke_style="solid"),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLabelsEnabled(False)


def _build_hillshade_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsBilinearRasterResampler, QgsSingleBandGrayRenderer
    from qgis.PyQt.QtGui import QPainter

    opacity, brightness, contrast = _hillshade_params(color_mode, carto.hillshade_strength)
    renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    renderer.setOpacity(opacity)
    layer.setRenderer(renderer)
    layer.setBlendMode(QPainter.CompositionMode_Multiply)

    if layer.resampleFilter() is not None:
        layer.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        layer.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())

    if layer.brightnessFilter() is not None:
        layer.brightnessFilter().setBrightness(brightness)
        layer.brightnessFilter().setContrast(contrast)


def _build_slope_shading_style(
    layer: Any,
    color_mode: ColorMode,
) -> None:
    """Style slope-angle avalanche shading raster (4-band RGBA or 1-band slope degrees).

    Lives on the lower map stack right above ``hillshade.tif`` with ``BlendNormal``
    so contours, trails, buildings, and the MGRS 1 km grid on the upper map stack
    remain 100% pure un-rasterized PDF vectors.
    """
    from qgis.core import (
        QgsBilinearRasterResampler,
        QgsColorRampShader,
        QgsMultiBandColorRenderer,
        QgsRasterShader,
        QgsSingleBandPseudoColorRenderer,
    )
    from qgis.PyQt.QtGui import QColor, QPainter

    bw = _is_bw(color_mode)
    band_count = layer.bandCount() if hasattr(layer, "bandCount") else 4

    if band_count >= 3:
        renderer = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
        if band_count >= 4:
            renderer.setAlphaBand(4)
        # ALPINE amplifies avalanche slope prominence; NIGHT_RED colorizes warm reds into
        # red-headlamp-safe deep indigo while preserving the band-4 alpha channel!
        if color_mode == ColorMode.ALPINE:
            renderer.setOpacity(0.68)
        elif color_mode == ColorMode.NIGHT_RED:
            renderer.setOpacity(0.62)
        elif bw:
            renderer.setOpacity(0.65)
        else:
            renderer.setOpacity(0.58)
        layer.setRenderer(renderer)
        if color_mode == ColorMode.NIGHT_RED and layer.hueSaturationFilter() is not None:
            hsf = layer.hueSaturationFilter()
            hsf.setColorizeOn(True)
            hsf.setColorizeColor(QColor("#1B3B6E"))
            hsf.setColorizeStrength(100)
    else:
        shader = QgsRasterShader()
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Discrete)
        if color_mode == ColorMode.NIGHT_RED:
            items = [
                QgsColorRampShader.ColorRampItem(26.999, QColor(0, 0, 0, 0), "< 27°"),
                QgsColorRampShader.ColorRampItem(29.999, QColor(93, 173, 226, 180), "27°–30°"),
                QgsColorRampShader.ColorRampItem(34.999, QColor(46, 109, 164, 205), "30°–35°"),
                QgsColorRampShader.ColorRampItem(44.999, QColor(40, 53, 147, 225), "35°–45°"),
                QgsColorRampShader.ColorRampItem(49.999, QColor(26, 35, 100, 235), "45°–50°"),
                QgsColorRampShader.ColorRampItem(90.0, QColor(12, 18, 56, 245), ">= 50°"),
            ]
        elif not bw:
            items = [
                QgsColorRampShader.ColorRampItem(26.999, QColor(0, 0, 0, 0), "< 27°"),
                QgsColorRampShader.ColorRampItem(29.999, QColor(246, 224, 94, 195), "27°–30°"),
                QgsColorRampShader.ColorRampItem(34.999, QColor(237, 137, 54, 215), "30°–35°"),
                QgsColorRampShader.ColorRampItem(44.999, QColor(229, 62, 62, 230), "35°–45°"),
                QgsColorRampShader.ColorRampItem(49.999, QColor(128, 90, 213, 235), "45°–50°"),
                QgsColorRampShader.ColorRampItem(90.0, QColor(59, 7, 100, 240), ">= 50°"),
            ]
        else:
            items = [
                QgsColorRampShader.ColorRampItem(29.999, QColor(0, 0, 0, 0), "< 30°"),
                QgsColorRampShader.ColorRampItem(34.999, QColor(120, 120, 120, 150), "30°–35°"),
                QgsColorRampShader.ColorRampItem(44.999, QColor(55, 55, 55, 195), "35°–45°"),
                QgsColorRampShader.ColorRampItem(90.0, QColor(15, 15, 15, 230), ">= 45°"),
            ]
        ramp.setColorRampItemList(items)
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        if color_mode == ColorMode.ALPINE:
            renderer.setOpacity(0.52)
        elif color_mode == ColorMode.NIGHT_RED:
            renderer.setOpacity(0.46)
        else:
            renderer.setOpacity(0.42 if not bw else 0.35)
        layer.setRenderer(renderer)

    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    if layer.resampleFilter() is not None:
        layer.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        layer.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())


def _build_viewshed_style(
    layer: Any,
    color_mode: ColorMode,
) -> None:
    """Style RF / line-of-sight viewshed coverage raster on the lower raster stack.

    Supports both 4-band RGBA Byte GeoTIFFs (from ``terrain.build_viewshed_products``)
    and 1-band binary visibility rasters (0/255), keeping ``BlendNormal`` right above
    ``hillshade.tif`` so all upper-stack vectors remain pure.
    """
    from qgis.core import (
        QgsBilinearRasterResampler,
        QgsColorRampShader,
        QgsMultiBandColorRenderer,
        QgsRasterShader,
        QgsSingleBandPseudoColorRenderer,
    )
    from qgis.PyQt.QtGui import QColor, QPainter

    bw = _is_bw(color_mode)
    band_count = layer.bandCount() if hasattr(layer, "bandCount") else 4
    eff_opacity = 0.58 if color_mode == ColorMode.NIGHT_RED else (0.55 if bw else 0.52)

    if band_count >= 3:
        renderer = QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3)
        if band_count >= 4:
            renderer.setAlphaBand(4)
        renderer.setOpacity(eff_opacity)
        layer.setRenderer(renderer)
    else:
        shader = QgsRasterShader()
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Discrete)
        if color_mode == ColorMode.NIGHT_RED:
            items = [
                QgsColorRampShader.ColorRampItem(0.5, QColor(0, 0, 0, 0), "Shadowed"),
                QgsColorRampShader.ColorRampItem(255.0, QColor(11, 130, 140, 180), "Visible"),
            ]
        elif not bw:
            items = [
                QgsColorRampShader.ColorRampItem(0.5, QColor(0, 0, 0, 0), "Shadowed"),
                QgsColorRampShader.ColorRampItem(255.0, QColor(0, 212, 146, 170), "Visible"),
            ]
        else:
            items = [
                QgsColorRampShader.ColorRampItem(0.5, QColor(0, 0, 0, 0), "Shadowed"),
                QgsColorRampShader.ColorRampItem(255.0, QColor(80, 80, 80, 145), "Visible"),
            ]
        ramp.setColorRampItemList(items)
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setOpacity(eff_opacity)
        layer.setRenderer(renderer)

    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    if layer.resampleFilter() is not None:
        layer.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        layer.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())


def _build_fema_flood_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style FEMA NFHL S_Fld_Haz_Ar polygons (SFHA 100-yr + moderate 500-yr flood zones)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    bw = _is_bw(color_mode)
    sfha_expr = (
        "upper(coalesce(\"sfha_tf\", '')) = 'T' "
        "OR upper(coalesce(\"fld_zone\", '')) LIKE 'A%' "
        "OR upper(coalesce(\"fld_zone\", '')) LIKE 'V%'"
    )
    mod_expr = (
        f"NOT ({sfha_expr}) AND ("
        "upper(coalesce(\"zone_subty\", '')) LIKE '%0.2%' "
        "OR upper(coalesce(\"fld_zone\", '')) IN ('B','X500')"
        ")"
    )

    root = QgsRuleBasedRenderer.Rule(None)
    if not bw:
        _add_rule(
            root,
            "FEMA SFHA (100-yr Flood Zone)",
            sfha_expr,
            _hatch_fill_symbol(
                _pal(color_mode, "fema_hatch"),
                angle_deg=45.0,
                spacing_mm=2.2,
                line_mm=0.15,
                bg_hex=_pal(color_mode, "fema_bg"),
                bg_alpha=34,
                outline_hex=_pal(color_mode, "fema_stroke"),
                outline_mm=0.18,
            ),
        )
        _add_rule(
            root,
            "FEMA 500-yr Flood Zone",
            mod_expr,
            _hatch_fill_symbol(
                _pal(color_mode, "fema_mod_hatch"),
                angle_deg=135.0,
                spacing_mm=3.4,
                line_mm=0.12,
                dash_vector=[1.4, 1.2],
                outline_hex=_pal(color_mode, "fema_mod_outline"),
                outline_mm=0.14,
                outline_dash=[1.4, 1.0],
            ),
        )
    else:
        _add_rule(
            root,
            "FEMA SFHA (100-yr Flood Zone)",
            sfha_expr,
            _hatch_fill_symbol(
                "#444444",
                angle_deg=45.0,
                spacing_mm=2.2,
                line_mm=0.15,
                dash_vector=[0.8, 1.0],
                outline_hex="#222222",
                outline_mm=0.18,
                outline_dash=[1.6, 1.0],
            ),
        )
        _add_rule(
            root,
            "FEMA 500-yr Flood Zone",
            mod_expr,
            _hatch_fill_symbol(
                "#777777",
                angle_deg=135.0,
                spacing_mm=3.5,
                line_mm=0.12,
                dash_vector=[0.6, 1.4],
            ),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    fd = scale_font_delta(scale)
    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "'ZONE ' || \"fld_zone\""
    pal.isExpression = True
    pal.priority = 2
    pal.setFormat(
        _make_text_format(
            5.6 + fd,
            _pal(color_mode, "fema_label"),
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.6,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("FEMA Zone Code")
    rule.setFilterExpression(f"({sfha_expr}) AND \"fld_zone\" IS NOT NULL AND length(\"fld_zone\") > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_water_polygons_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    water_filter = (
        "\"natural\" = 'water' OR \"water\" IS NOT NULL "
        "OR \"waterway\" IN ('riverbank','dock','canal') "
        "OR \"landuse\" IN ('reservoir','basin')"
    )
    root = QgsRuleBasedRenderer.Rule(None)
    if not _is_bw(color_mode):
        sym = _simple_fill_symbol(
            _pal(color_mode, "water_fill"),
            stroke_hex=_pal(color_mode, "water_stroke"),
            stroke_mm=0.20 * wf,
            stroke_style="solid",
        )
    else:
        sym = _hatch_fill_symbol(
            "#6B6B6B",
            angle_deg=0.0,
            spacing_mm=1.25,
            line_mm=0.12,
            bg_hex="#FFFFFF",
            outline_hex="#1A1A1A",
            outline_mm=0.26 * wf,
        )
    _add_rule(root, "Waterbodies", water_filter, sym)
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 5
    pal.setFormat(
        _make_text_format(
            6.5 + fd,
            _pal(color_mode, "water_label"),
            italic=True,
            halo_hex="#FFFFFF",
            halo_mm=0.6,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Waterbody Names")
    rule.setFilterExpression(f"({water_filter}) AND \"name\" IS NOT NULL AND length(\"name\") > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(carto.label_density != DensityLevel.LOW)


def _build_contours_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)

    root = QgsRuleBasedRenderer.Rule(None)
    idx_color = _pal(color_mode, "contour_idx")
    minor_color = _pal(color_mode, "contour_minor")
    label_color = _pal(color_mode, "contour_label")
    halo_color = _pal(color_mode, "halo")
    if color_mode == ColorMode.NIGHT_RED:
        idx_w, minor_w = 0.34 * wf, 0.16 * wf
    elif color_mode == ColorMode.ALPINE:
        idx_w, minor_w = 0.33 * wf, 0.15 * wf
    elif color_mode == ColorMode.COLOR:
        idx_w, minor_w = 0.32 * wf, 0.15 * wf
    else:
        idx_w, minor_w = 0.30 * wf, 0.14 * wf

    _add_rule(root, "Index Contours", "\"is_index\" = 1", _line_symbol(idx_color, idx_w))
    _add_rule(
        root,
        "Intermediate Contours",
        "\"is_index\" = 0 OR \"is_index\" IS NULL",
        _line_symbol(minor_color, minor_w),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "elev_label"
    pal.isExpression = False
    pal.priority = 3
    _configure_curved_line_pal(
        pal,
        on_line=True,
        repeat_mm=55.0 * rep_mult,
        min_feature_mm=12.0 * min_mult,
    )
    pal.setFormat(
        _make_text_format(
            5.8 + fd,
            label_color,
            bold=True,
            halo_hex=halo_color,
            halo_mm=0.65,
            halo_opacity=0.95,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Index Contour Elevations")
    rule.setFilterExpression("\"is_index\" = 1 AND \"elev_label\" IS NOT NULL")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_enc_depth_contours_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style NOAA ENC depth contours (``enc_depth_contours`` with ``valdco``)."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    c_hex = _pal(color_mode, "enc_depth")

    layer.setRenderer(QgsSingleSymbolRenderer(_line_symbol(c_hex, 0.22 * wf, dash_vector=[2.2, 1.0])))

    pal = QgsPalLayerSettings()
    pal.fieldName = "CASE WHEN \"valdco\" IS NOT NULL THEN round(to_real(\"valdco\"), 1) || ' m' ELSE '' END"
    pal.isExpression = True
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=True, repeat_mm=60.0 * rep_mult, min_feature_mm=10.0 * min_mult)
    pal.setFormat(_make_text_format(5.5 + fd, c_hex, italic=True, halo_hex="#FFFFFF", halo_mm=0.55))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_nhd_flowlines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style USGS NHD flowlines (perennial, intermittent, ephemeral, canal)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    arrow_iv = 34.0 / wf

    perennial_c = _pal(color_mode, "nhd_perennial")
    intermit_c = _pal(color_mode, "nhd_intermit")
    ephem_c = _pal(color_mode, "nhd_ephem")
    halo_c = _pal(color_mode, "halo")

    root = QgsRuleBasedRenderer.Rule(None)
    _add_rule(
        root,
        "NHD Perennial Stream",
        "\"flow_type\" = 'perennial'",
        _waterway_symbol(
            perennial_c,
            0.28 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=1.7 * wf,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "NHD Intermittent Stream",
        "\"flow_type\" = 'intermittent'",
        _waterway_symbol(
            intermit_c,
            0.22 * wf,
            dash_vector=[2.6, 0.9, 0.5, 0.9],
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv * 1.25,
            arrow_size_mm=1.5 * wf,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "NHD Ephemeral Stream",
        "\"flow_type\" = 'ephemeral'",
        _line_symbol(ephem_c, 0.18 * wf, dash_vector=[0.8, 1.0]),
    )
    _add_rule(
        root,
        "NHD Canal / Other",
        "\"flow_type\" IS NULL OR \"flow_type\" NOT IN ('perennial','intermittent','ephemeral')",
        _waterway_symbol(
            perennial_c,
            0.26 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=1.6 * wf,
            icons_root=icons_root,
        ),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "gnis_name"
    pal.isExpression = False
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=70.0 * rep_mult, min_feature_mm=15.0 * min_mult)
    pal.setFormat(_make_text_format(5.8 + fd, perennial_c, italic=True, halo_hex=halo_c, halo_mm=0.55))
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("NHD Stream Names")
    rule.setFilterExpression("\"gnis_name\" IS NOT NULL AND length(\"gnis_name\") > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_waterways_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    arrow_iv = 30.0 / wf

    root = QgsRuleBasedRenderer.Rule(None)
    river_c = _pal(color_mode, "river")
    stream_c = _pal(color_mode, "stream")
    label_c = _pal(color_mode, "water_label")
    halo_c = _pal(color_mode, "halo")

    _add_rule(
        root,
        "Rivers & Canals",
        "\"waterway\" IN ('river','canal') AND (\"intermittent\" IS NULL OR \"intermittent\" != 'yes')",
        _waterway_symbol(
            river_c,
            0.55 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=2.1 * wf,
            arrow_color_hex=label_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "Streams & Creeks",
        "\"waterway\" IN ('stream','creek','drain','ditch') AND (\"intermittent\" IS NULL OR \"intermittent\" != 'yes')",
        _waterway_symbol(
            stream_c,
            0.24 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv * 1.15,
            arrow_size_mm=1.65 * wf,
            arrow_color_hex=label_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "Intermittent Waterways",
        "\"waterway\" IN ('river','stream','creek','canal','drain','ditch') AND \"intermittent\" = 'yes'",
        _line_symbol(stream_c, 0.22 * wf, dash_vector=[2.8, 0.9, 0.5, 0.9]),
    )
    _add_rule(
        root,
        "Dams & Weirs",
        "\"waterway\" IN ('dam','weir','waterfall')",
        _line_symbol(river_c, 0.45 * wf, cap_round=False),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=75.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    pal.setFormat(_make_text_format(6.0 + fd, label_c, italic=True, halo_hex=halo_c, halo_mm=0.55))
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Waterway Names")
    rule.setFilterExpression(
        "\"waterway\" IN ('river','stream','creek','canal') AND \"name\" IS NOT NULL AND length(\"name\") > 0"
    )
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_buildings_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsRuleBasedRenderer

    root = QgsRuleBasedRenderer.Rule(None)

    if carto.show_buildings:
        sym = _simple_fill_symbol(
            _pal(color_mode, "building_fill"),
            stroke_hex=_pal(color_mode, "building_stroke"),
            stroke_mm=0.10,
            stroke_style="solid",
        )
        _add_rule(root, "Buildings", "\"building\" IS NOT NULL AND \"building\" != 'no'", sym)

    if carto.show_power:
        sub_sym = _hatch_fill_symbol(
            _pal(color_mode, "substation_hatch"),
            angle_deg=135.0,
            spacing_mm=1.6,
            line_mm=0.12,
            bg_hex=_pal(color_mode, "substation_bg"),
            outline_hex=_pal(color_mode, "substation_outline"),
            outline_mm=0.22,
        )
        _add_rule(root, "Power Substations", "\"power\" = 'substation'", sub_sym)

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLabelsEnabled(False)


def _build_building_icons_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Render military/topo usage icons at the interior centroid of tagged OSM building polygons."""
    from qgis.core import QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    ink = _pal(color_mode, "ink")
    red = _pal(color_mode, "danger")
    blue = _pal(color_mode, "accent_blue")

    root = QgsRuleBasedRenderer.Rule(None)
    if carto.show_buildings:
        _add_rule(
            root,
            "Hospital Buildings",
            "\"amenity\" IN ('hospital','clinic') OR \"building\" = 'hospital' OR \"emergency\" = 'ambulance_station'",
            _centroid_svg_fill_symbol("hospital", 2.7 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fire Station Buildings",
            "\"amenity\" = 'fire_station' OR \"emergency\" = 'fire_station'",
            _centroid_svg_fill_symbol("fire_station", 2.6 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Police Buildings",
            "\"amenity\" = 'police'",
            _centroid_svg_fill_symbol("police", 2.6 * wf, fill_hex=blue, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "School / University Buildings",
            "\"amenity\" IN ('school','college','university') OR \"building\" = 'school'",
            _centroid_svg_fill_symbol("school", 2.5 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Place of Worship Buildings",
            "\"amenity\" = 'place_of_worship' OR \"building\" IN ('church','chapel','cathedral','mosque','temple','synagogue')",
            _centroid_svg_fill_symbol("worship", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fuel Station Buildings",
            "\"amenity\" = 'fuel'",
            _centroid_svg_fill_symbol("fuel", 2.3 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Shelter / Hut Buildings",
            "\"amenity\" = 'shelter' OR \"building\" = 'hut'",
            _centroid_svg_fill_symbol("shelter", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )

    _add_rule(
        root,
        "Helipad Polygons",
        "\"aeroway\" = 'helipad'",
        _centroid_svg_fill_symbol("helipad", 2.8 * wf, fill_hex=blue, outline_hex=blue, icons_root=icons_root),
    )

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setLabelsEnabled(False)


def _build_transport_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    bw = _is_bw(color_mode)

    root = QgsRuleBasedRenderer.Rule(None)
    if not bw:
        _add_rule(
            root,
            "Motorway / Trunk",
            "\"highway\" IN ('motorway','motorway_link','trunk','trunk_link')",
            _cased_road_symbol(
                _pal(color_mode, "motorway_casing"),
                1.40 * wf,
                _pal(color_mode, "motorway_fill"),
                0.95 * wf,
                casing_pass=12,
                fill_pass=24,
                centerline_hex=_pal(color_mode, "motorway_center"),
                centerline_mm=0.14 * wf,
                centerline_pass=26,
            ),
        )
        _add_rule(
            root,
            "Primary Road",
            "\"highway\" IN ('primary','primary_link')",
            _cased_road_symbol(
                _pal(color_mode, "primary_casing"),
                1.15 * wf,
                _pal(color_mode, "primary_fill"),
                0.75 * wf,
                casing_pass=10,
                fill_pass=22,
            ),
        )
        _add_rule(
            root,
            "Secondary Road",
            "\"highway\" IN ('secondary','secondary_link')",
            _cased_road_symbol(
                _pal(color_mode, "secondary_casing"),
                0.95 * wf,
                _pal(color_mode, "secondary_fill"),
                0.60 * wf,
                casing_pass=8,
                fill_pass=20,
            ),
        )
        _add_rule(
            root,
            "Tertiary / Residential",
            "\"highway\" IN ('tertiary','tertiary_link','unclassified','residential','living_street','road')",
            _cased_road_symbol(
                _pal(color_mode, "tertiary_casing"),
                0.72 * wf,
                _pal(color_mode, "tertiary_fill"),
                0.44 * wf,
                casing_pass=6,
                fill_pass=18,
            ),
        )
        _add_rule(
            root,
            "Service Road",
            "\"highway\" = 'service'",
            _cased_road_symbol(
                _pal(color_mode, "service_casing"),
                0.46 * wf,
                _pal(color_mode, "service_fill"),
                0.26 * wf,
                casing_pass=4,
                fill_pass=16,
            ),
        )
        _add_rule(
            root,
            "Unpaved Track (Graded)",
            "\"highway\" = 'track' AND (\"tracktype\" IS NULL OR \"tracktype\" NOT IN ('grade4','grade5'))",
            _line_symbol(_pal(color_mode, "track"), 0.34 * wf, dash_vector=[2.2, 1.0], render_pass=5),
        )
        _add_rule(
            root,
            "Rough 4WD Track (Grade 4-5)",
            "\"highway\" = 'track' AND \"tracktype\" IN ('grade4','grade5')",
            _line_symbol(
                _pal(color_mode, "track_rough"),
                0.30 * wf,
                dash_vector=[1.4, 0.7, 0.4, 0.7],
                render_pass=5,
            ),
        )
        _add_rule(
            root,
            "Trails / Footways / Paths",
            "\"highway\" IN ('path','footway','bridleway','cycleway')",
            _line_symbol(_pal(color_mode, "trail"), 0.24 * wf, dash_vector=[1.1, 0.9], render_pass=3),
        )
        _add_rule(
            root,
            "Steps / Stairways",
            "\"highway\" = 'steps'",
            _line_symbol(_pal(color_mode, "trail"), 0.36 * wf, dash_vector=[0.55, 0.45], cap_round=False, render_pass=4),
        )
        _add_rule(
            root,
            "Cliffs & Escarpments",
            "\"natural\" = 'cliff'",
            _cliff_symbol(_pal(color_mode, "cliff"), render_pass=4, wf=wf),
        )
        _add_rule(
            root,
            "Stream Fords",
            "\"ford\" = 'yes'",
            _line_symbol(_pal(color_mode, "river"), 0.48 * wf, dash_vector=[0.65, 0.55], render_pass=25),
        )
        if scale <= 25000:
            _add_rule(
                root,
                "Walls & Retaining Walls",
                "\"barrier\" IN ('wall','retaining_wall','city_wall')",
                _line_symbol(_pal(color_mode, "barrier"), 0.22 * wf, cap_round=False, render_pass=2),
            )
        if scale <= 15000:
            _add_rule(
                root,
                "Fences",
                "\"barrier\" = 'fence'",
                _line_symbol(_pal(color_mode, "barrier"), 0.15 * wf, dash_vector=[1.3, 0.65], render_pass=1),
            )
        _add_rule(
            root,
            "Railway",
            "\"railway\" IN ('rail','light_rail','narrow_gauge','preserved','tram')",
            _railway_symbol(_pal(color_mode, "rail"), base_pass=15, wf=wf),
        )
        _add_rule(
            root,
            "Runways",
            "\"aeroway\" = 'runway'",
            _cased_road_symbol(
                _pal(color_mode, "rail"),
                1.65 * wf,
                _pal(color_mode, "runway_fill"),
                1.20 * wf,
                casing_pass=9,
                fill_pass=21,
                centerline_hex="#FFFFFF",
                centerline_mm=0.20 * wf,
                centerline_dash=[1.8, 1.2],
                centerline_pass=27,
            ),
        )
        _add_rule(
            root,
            "Taxiways",
            "\"aeroway\" = 'taxiway'",
            _cased_road_symbol(
                _pal(color_mode, "taxiway_casing"),
                0.85 * wf,
                _pal(color_mode, "taxiway_fill"),
                0.55 * wf,
                casing_pass=7,
                fill_pass=19,
            ),
        )
        if carto.show_power:
            _add_rule(
                root,
                "Power Lines",
                "\"power\" IN ('line','minor_line')",
                _powerline_symbol(_pal(color_mode, "power"), render_pass=2, wf=wf),
            )
        if carto.show_pipelines:
            _add_rule(
                root,
                "Pipelines",
                "\"man_made\" = 'pipeline'",
                _pipeline_symbol(_pal(color_mode, "pipeline"), render_pass=2, wf=wf),
            )
    else:
        _add_rule(
            root,
            "Motorway / Trunk",
            "\"highway\" IN ('motorway','motorway_link','trunk','trunk_link')",
            _cased_road_symbol(
                "#000000",
                1.45 * wf,
                "#FFFFFF",
                0.88 * wf,
                casing_pass=12,
                fill_pass=24,
                centerline_hex="#000000",
                centerline_mm=0.22 * wf,
                centerline_pass=26,
            ),
        )
        _add_rule(
            root,
            "Primary Road",
            "\"highway\" IN ('primary','primary_link')",
            _cased_road_symbol("#000000", 1.15 * wf, "#C8C8C8", 0.68 * wf, casing_pass=10, fill_pass=22),
        )
        _add_rule(
            root,
            "Secondary Road",
            "\"highway\" IN ('secondary','secondary_link')",
            _cased_road_symbol("#000000", 0.95 * wf, "#FFFFFF", 0.56 * wf, casing_pass=8, fill_pass=20),
        )
        _add_rule(
            root,
            "Tertiary / Residential",
            "\"highway\" IN ('tertiary','tertiary_link','unclassified','residential','living_street','road')",
            _cased_road_symbol("#222222", 0.68 * wf, "#FFFFFF", 0.40 * wf, casing_pass=6, fill_pass=18),
        )
        _add_rule(
            root,
            "Service Road",
            "\"highway\" = 'service'",
            _line_symbol("#444444", 0.28 * wf, render_pass=4),
        )
        _add_rule(
            root,
            "Unpaved Track (Graded)",
            "\"highway\" = 'track' AND (\"tracktype\" IS NULL OR \"tracktype\" NOT IN ('grade4','grade5'))",
            _line_symbol("#1A1A1A", 0.34 * wf, dash_vector=[2.2, 1.0], render_pass=5),
        )
        _add_rule(
            root,
            "Rough 4WD Track (Grade 4-5)",
            "\"highway\" = 'track' AND \"tracktype\" IN ('grade4','grade5')",
            _line_symbol("#2A2A2A", 0.30 * wf, dash_vector=[1.4, 0.7, 0.4, 0.7], render_pass=5),
        )
        _add_rule(
            root,
            "Trails / Footways / Paths",
            "\"highway\" IN ('path','footway','bridleway','cycleway')",
            _line_symbol("#1A1A1A", 0.24 * wf, dash_vector=[0.85, 0.85], render_pass=3),
        )
        _add_rule(
            root,
            "Steps / Stairways",
            "\"highway\" = 'steps'",
            _line_symbol("#1A1A1A", 0.36 * wf, dash_vector=[0.55, 0.45], cap_round=False, render_pass=4),
        )
        _add_rule(
            root,
            "Cliffs & Escarpments",
            "\"natural\" = 'cliff'",
            _cliff_symbol("#1A1A1A", render_pass=4, wf=wf),
        )
        _add_rule(
            root,
            "Stream Fords",
            "\"ford\" = 'yes'",
            _line_symbol("#1A1A1A", 0.48 * wf, dash_vector=[0.65, 0.55], render_pass=25),
        )
        if scale <= 25000:
            _add_rule(
                root,
                "Walls & Retaining Walls",
                "\"barrier\" IN ('wall','retaining_wall','city_wall')",
                _line_symbol("#4A4A4A", 0.22 * wf, cap_round=False, render_pass=2),
            )
        if scale <= 15000:
            _add_rule(
                root,
                "Fences",
                "\"barrier\" = 'fence'",
                _line_symbol("#4A4A4A", 0.15 * wf, dash_vector=[1.3, 0.65], render_pass=1),
            )
        _add_rule(
            root,
            "Railway",
            "\"railway\" IN ('rail','light_rail','narrow_gauge','preserved','tram')",
            _railway_symbol("#000000", base_pass=15, wf=wf),
        )
        _add_rule(
            root,
            "Runways",
            "\"aeroway\" = 'runway'",
            _cased_road_symbol(
                "#000000",
                1.65 * wf,
                "#444444",
                1.20 * wf,
                casing_pass=9,
                fill_pass=21,
                centerline_hex="#FFFFFF",
                centerline_mm=0.22 * wf,
                centerline_dash=[1.8, 1.2],
                centerline_pass=27,
            ),
        )
        _add_rule(
            root,
            "Taxiways",
            "\"aeroway\" = 'taxiway'",
            _cased_road_symbol("#000000", 0.85 * wf, "#BDBDBD", 0.55 * wf, casing_pass=7, fill_pass=19),
        )
        if carto.show_power:
            _add_rule(
                root,
                "Power Lines",
                "\"power\" IN ('line','minor_line')",
                _powerline_symbol("#333333", render_pass=2, wf=wf),
            )
        if carto.show_pipelines:
            _add_rule(
                root,
                "Pipelines",
                "\"man_made\" = 'pipeline'",
                _pipeline_symbol("#1A1A1A", render_pass=2, wf=wf),
            )

    renderer = QgsRuleBasedRenderer(root)
    renderer.setUsingSymbolLevels(True)
    layer.setRenderer(renderer)

    # Hierarchical road labeling (shield badge for highway ref + curved street names)
    halo_hex = _pal(color_mode, "halo")
    label_root = QgsRuleBasedLabeling.Rule(None)

    # Highway route shield badge on motorway/trunk/primary with ref
    pal_shield = QgsPalLayerSettings()
    pal_shield.fieldName = "trim(array_first(string_to_array(\"ref\", ';')))"
    pal_shield.isExpression = True
    pal_shield.priority = 8
    _configure_curved_line_pal(pal_shield, on_line=True, repeat_mm=80.0 * rep_mult, min_feature_mm=18.0 * min_mult)
    pal_shield.setFormat(
        _make_text_format(
            5.8 + fd,
            _pal(color_mode, "ink_dark"),
            bold=True,
            badge_fill_hex="#FFFFFF",
            badge_stroke_hex=_pal(color_mode, "shield_stroke"),
        )
    )
    r_shield = QgsRuleBasedLabeling.Rule(pal_shield)
    r_shield.setDescription("Highway Route Shields")
    r_shield.setFilterExpression(
        "\"highway\" IN ('motorway','trunk','primary') AND \"ref\" IS NOT NULL AND length(\"ref\") > 0"
    )
    label_root.appendChild(r_shield)

    pal_major = QgsPalLayerSettings()
    pal_major.fieldName = (
        "CASE WHEN \"name\" IS NOT NULL AND length(\"name\") > 0 THEN \"name\" "
        "ELSE \"ref\" END"
    )
    pal_major.isExpression = True
    pal_major.priority = 7
    _configure_curved_line_pal(pal_major, on_line=False, repeat_mm=65.0 * rep_mult, min_feature_mm=14.0 * min_mult)
    pal_major.setFormat(_make_text_format(6.4 + fd, _pal(color_mode, "ink"), bold=True, halo_hex=halo_hex, halo_mm=0.7))
    r_major = QgsRuleBasedLabeling.Rule(pal_major)
    r_major.setDescription("Major Road Names")
    r_major.setFilterExpression(
        "\"highway\" IN ('motorway','trunk','primary','secondary') "
        "AND ((\"name\" IS NOT NULL AND length(\"name\") > 0) OR (\"ref\" IS NOT NULL AND length(\"ref\") > 0))"
    )
    label_root.appendChild(r_major)

    if carto.label_density != DensityLevel.LOW:
        minor_highways = (
            "('tertiary','unclassified','residential','service','track','path','footway')"
            if carto.label_density == DensityLevel.HIGH
            else "('tertiary','unclassified','residential','track','path','footway')"
        )
        pal_minor = QgsPalLayerSettings()
        pal_minor.fieldName = "name"
        pal_minor.isExpression = False
        pal_minor.priority = 5
        _configure_curved_line_pal(
            pal_minor,
            on_line=False,
            repeat_mm=70.0 * rep_mult,
            min_feature_mm=15.0 * min_mult,
        )
        pal_minor.setFormat(
            _make_text_format(5.6 + fd, _pal(color_mode, "ink_soft"), halo_hex=halo_hex, halo_mm=0.6)
        )
        r_minor = QgsRuleBasedLabeling.Rule(pal_minor)
        r_minor.setDescription("Minor Road & Trail Labels")
        r_minor.setFilterExpression(
            f"\"highway\" IN {minor_highways} AND \"name\" IS NOT NULL AND length(\"name\") > 0"
        )
        label_root.appendChild(r_minor)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_points_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    halo_hex = _pal(color_mode, "halo")
    ink = _pal(color_mode, "ink")
    red = _pal(color_mode, "danger")
    blue = _pal(color_mode, "accent_blue")

    root = QgsRuleBasedRenderer.Rule(None)

    # Peaks & Volcanoes
    peak_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "triangle",
            "color": _pal(color_mode, "ink_dark"),
            "outline_color": "#FFFFFF",
            "outline_width": "0.28",
            "outline_width_unit": "MM",
            "size": f"{2.3 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Peaks & Summits", "\"natural\" IN ('peak','volcano')", peak_sym)

    # Saddles / Mountain Passes
    _add_rule(
        root,
        "Saddles & Passes",
        "\"natural\" = 'saddle'",
        _svg_marker_symbol("saddle", 2.3 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
    )

    # Highway Exits / Motorway Junctions
    exit_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#FFFFFF",
            "outline_color": _pal(color_mode, "exit_stroke"),
            "outline_width": "0.42",
            "outline_width_unit": "MM",
            "size": f"{1.9 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Highway Exits", "\"highway\" = 'motorway_junction'", exit_sym)

    # Helipads & Aerodromes
    _add_rule(
        root,
        "Helipads",
        "\"aeroway\" = 'helipad'",
        _svg_marker_symbol("helipad", 2.8 * wf, fill_hex=blue, outline_hex=blue, icons_root=icons_root),
    )
    aero_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": "#FFFFFF",
            "outline_color": _pal(color_mode, "ink_dark"),
            "outline_width": "0.35",
            "outline_width_unit": "MM",
            "size": f"{2.2 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Aerodromes", "\"aeroway\" = 'aerodrome'", aero_sym)

    # Building / POI Usage SVG Icons
    if carto.show_buildings:
        _add_rule(
            root,
            "Hospitals",
            "\"amenity\" IN ('hospital','clinic') OR \"emergency\" = 'ambulance_station' OR \"building\" = 'hospital'",
            _svg_marker_symbol("hospital", 2.7 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fire Stations",
            "\"amenity\" = 'fire_station' OR \"emergency\" = 'fire_station'",
            _svg_marker_symbol("fire_station", 2.6 * wf, fill_hex=red, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Police Stations",
            "\"amenity\" = 'police'",
            _svg_marker_symbol("police", 2.6 * wf, fill_hex=blue, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Schools & Universities",
            "\"amenity\" IN ('school','college','university') OR \"building\" = 'school'",
            _svg_marker_symbol("school", 2.5 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Places of Worship",
            "\"amenity\" = 'place_of_worship' OR \"building\" IN ('church','chapel','cathedral','mosque','temple','synagogue')",
            _svg_marker_symbol("worship", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Fuel Stations",
            "\"amenity\" = 'fuel'",
            _svg_marker_symbol("fuel", 2.3 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )
        _add_rule(
            root,
            "Shelters & Huts",
            "\"amenity\" = 'shelter' OR \"tourism\" IN ('alpine_hut','wilderness_hut','camp_site') OR \"building\" = 'hut'",
            _svg_marker_symbol("shelter", 2.4 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
        )

    # Communications Towers & Masts
    _add_rule(
        root,
        "Communications Towers & Masts",
        "\"man_made\" IN ('tower','mast','communications_tower','water_tower','lighthouse') "
        "OR \"tower:type\" IN ('communication','radio','radar')",
        _svg_marker_symbol("comm_tower", 2.5 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
    )

    # Power Towers (at detailed scales <= 1:25k when show_power is enabled)
    if carto.show_power and scale <= 25000:
        pylon_sym = QgsMarkerSymbol.createSimple(
            {
                "name": "cross2",
                "color": _pal(color_mode, "pylon"),
                "outline_color": _pal(color_mode, "pylon"),
                "outline_width": "0.22",
                "outline_width_unit": "MM",
                "size": f"{1.1 * wf:.3f}",
                "size_unit": "MM",
            }
        )
        _add_rule(root, "Power Towers", "\"power\" = 'tower'", pylon_sym)

    # Viewpoints & Railway Stations
    vp_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "diamond",
            "color": _pal(color_mode, "viewpoint"),
            "outline_color": "#FFFFFF",
            "outline_width": "0.22",
            "outline_width_unit": "MM",
            "size": f"{1.5 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Viewpoints", "\"tourism\" = 'viewpoint'", vp_sym)

    station_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": ink,
            "outline_color": "#FFFFFF",
            "outline_width": "0.25",
            "outline_width_unit": "MM",
            "size": f"{1.6 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Railway Stations", "\"railway\" IN ('station','halt')", station_sym)

    # Backcountry Water & Terrain Micro-Features
    waterfall_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "diamond",
            "color": _pal(color_mode, "river"),
            "outline_color": "#FFFFFF",
            "outline_width": "0.26",
            "outline_width_unit": "MM",
            "size": f"{1.75 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Waterfalls & Rapids", "\"waterway\" IN ('waterfall','rapids')", waterfall_sym)

    spring_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": "#FFFFFF",
            "outline_color": _pal(color_mode, "stream"),
            "outline_width": "0.34",
            "outline_width_unit": "MM",
            "size": f"{1.35 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Natural Springs", "\"natural\" = 'spring'", spring_sym)

    ford_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#FFFFFF",
            "outline_color": _pal(color_mode, "river"),
            "outline_width": "0.32",
            "outline_width_unit": "MM",
            "size": f"{1.45 * wf:.3f}",
            "size_unit": "MM",
            "angle": "45",
        }
    )
    _add_rule(root, "Fords", "\"ford\" = 'yes' OR \"highway\" = 'ford'", ford_sym)

    cave_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "semi_circle",
            "color": _pal(color_mode, "cliff"),
            "outline_color": "#FFFFFF",
            "outline_width": "0.22",
            "outline_width_unit": "MM",
            "size": f"{1.65 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Cave Entrances", "\"natural\" = 'cave_entrance'", cave_sym)

    trig_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "triangle",
            "color": "#FFFFFF",
            "outline_color": _pal(color_mode, "ink_dark"),
            "outline_width": "0.34",
            "outline_width_unit": "MM",
            "size": f"{2.0 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Survey Points & Trigs", "\"man_made\" = 'survey_point'", trig_sym)

    ruins_sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#FFFFFF",
            "outline_color": _pal(color_mode, "ink_soft"),
            "outline_width": "0.30",
            "outline_width_unit": "MM",
            "size": f"{1.45 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    _add_rule(root, "Historic Ruins & Forts", "\"historic\" IN ('ruins','castle','fort')", ruins_sym)

    if scale <= 25000:
        cairn_sym = QgsMarkerSymbol.createSimple(
            {
                "name": "circle",
                "color": _pal(color_mode, "cliff"),
                "outline_color": "#FFFFFF",
                "outline_width": "0.18",
                "outline_width_unit": "MM",
                "size": f"{1.10 * wf:.3f}",
                "size_unit": "MM",
            }
        )
        _add_rule(root, "Cairns & Rock Outcrops", "\"man_made\" = 'cairn' OR \"natural\" = 'rock'", cairn_sym)

    if scale <= 15000:
        gate_sym = QgsMarkerSymbol.createSimple(
            {
                "name": "cross2",
                "color": _pal(color_mode, "barrier"),
                "outline_color": _pal(color_mode, "barrier"),
                "outline_width": "0.24",
                "outline_width_unit": "MM",
                "size": f"{1.05 * wf:.3f}",
                "size_unit": "MM",
            }
        )
        _add_rule(
            root,
            "Gates, Stiles & Cattle Grids",
            "\"barrier\" IN ('gate','stile','kissing_gate','cattle_grid','lift_gate')",
            gate_sym,
        )

    place_sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0,0", "outline_style": "no", "size": "0.01"})
    _add_rule(
        root,
        "Places",
        "\"place\" IN ('city','town','village','suburb','hamlet','locality')",
        place_sym,
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    # Hierarchical point labeling
    label_root = QgsRuleBasedLabeling.Rule(None)

    # 1. Cities & Towns (highest priority)
    pal_town = QgsPalLayerSettings()
    pal_town.fieldName = "upper(\"name\")"
    pal_town.isExpression = True
    pal_town.priority = 10
    _configure_point_pal(pal_town, over_point=True)
    pal_town.setFormat(_make_text_format(9.0 + fd, _pal(color_mode, "ink_dark"), bold=True, halo_hex=halo_hex, halo_mm=0.95))
    r_town = QgsRuleBasedLabeling.Rule(pal_town)
    r_town.setDescription("Cities & Towns")
    r_town.setFilterExpression("\"place\" IN ('city','town') AND \"name\" IS NOT NULL")
    label_root.appendChild(r_town)

    # 2. Villages & Suburbs
    pal_village = QgsPalLayerSettings()
    pal_village.fieldName = "name"
    pal_village.isExpression = False
    pal_village.priority = 9
    _configure_point_pal(pal_village, over_point=True)
    pal_village.setFormat(_make_text_format(7.6 + fd, ink, bold=True, halo_hex=halo_hex, halo_mm=0.85))
    r_village = QgsRuleBasedLabeling.Rule(pal_village)
    r_village.setDescription("Villages & Suburbs")
    r_village.setFilterExpression("\"place\" IN ('village','suburb') AND \"name\" IS NOT NULL")
    label_root.appendChild(r_village)

    # 3. Peaks (name + deduplicated elevation in metric or imperial)
    num_ele = "to_real(regexp_substr(\"ele\", '-?[0-9]+(?:\\\\.[0-9]+)?'))"
    if units == Units.IMPERIAL:
        rounded_val = f"to_int(round({num_ele} * 3.280839895))"
        ele_expr = f"{rounded_val} || ' ft'"
        unit_pat = "(?:ft|feet)"
    else:
        rounded_val = f"to_int(round({num_ele}))"
        ele_expr = f"{rounded_val} || ' m'"
        unit_pat = "m(?:eters?|etres?)?"
    already_has_ele = (
        f"regexp_match(\"name\", '(?i)(?:^|[^0-9])' || to_string({rounded_val}) || '\\\\s*{unit_pat}\\\\b') > 0"
    )
    peak_expr = (
        f"CASE "
        f"WHEN \"name\" IS NOT NULL AND \"ele\" IS NOT NULL AND {num_ele} IS NOT NULL "
        f"THEN CASE WHEN {already_has_ele} THEN \"name\" ELSE \"name\" || '\\n' || {ele_expr} END "
        f"WHEN \"name\" IS NOT NULL THEN \"name\" "
        f"WHEN \"ele\" IS NOT NULL AND {num_ele} IS NOT NULL THEN {ele_expr} "
        f"ELSE '' END"
    )
    pal_peak = QgsPalLayerSettings()
    pal_peak.fieldName = peak_expr
    pal_peak.isExpression = True
    pal_peak.priority = 8
    _configure_point_pal(pal_peak, over_point=False, dist_mm=1.6)
    pal_peak.setFormat(_make_text_format(6.8 + fd, _pal(color_mode, "ink_dark"), bold=True, halo_hex=halo_hex, halo_mm=0.75))
    r_peak = QgsRuleBasedLabeling.Rule(pal_peak)
    r_peak.setDescription("Peaks & Elevations")
    r_peak.setFilterExpression("\"natural\" IN ('peak','volcano') AND (\"name\" IS NOT NULL OR \"ele\" IS NOT NULL)")
    label_root.appendChild(r_peak)

    # 4. Highway Exit Numbers
    pal_exit = QgsPalLayerSettings()
    pal_exit.fieldName = (
        "CASE WHEN \"ref\" IS NOT NULL AND length(\"ref\") > 0 THEN 'Exit ' || \"ref\" "
        "WHEN \"name\" IS NOT NULL THEN \"name\" ELSE '' END"
    )
    pal_exit.isExpression = True
    pal_exit.priority = 8
    _configure_point_pal(pal_exit, over_point=False, dist_mm=1.4)
    pal_exit.setFormat(
        _make_text_format(
            6.0 + fd,
            _pal(color_mode, "exit_text"),
            bold=True,
            badge_fill_hex="#FFFFFF",
            badge_stroke_hex=_pal(color_mode, "exit_stroke"),
        )
    )
    r_exit = QgsRuleBasedLabeling.Rule(pal_exit)
    r_exit.setDescription("Highway Exits")
    r_exit.setFilterExpression(
        "\"highway\" = 'motorway_junction' AND (\"ref\" IS NOT NULL OR \"name\" IS NOT NULL)"
    )
    label_root.appendChild(r_exit)

    # 5. Localities, Viewpoints, Stations, Saddles (suppressed at LOW label density)
    if carto.label_density != DensityLevel.LOW:
        pal_local = QgsPalLayerSettings()
        pal_local.fieldName = "name"
        pal_local.isExpression = False
        pal_local.priority = 6
        _configure_point_pal(pal_local, over_point=False, dist_mm=1.3)
        pal_local.setFormat(
            _make_text_format(6.4 + fd, _pal(color_mode, "ink_soft"), halo_hex=halo_hex, halo_mm=0.65)
        )
        r_local = QgsRuleBasedLabeling.Rule(pal_local)
        r_local.setDescription("Localities / Viewpoints / Stations / Saddles / Backcountry POIs")
        r_local.setFilterExpression(
            "(\"place\" IN ('hamlet','locality') OR \"tourism\" IN ('viewpoint','alpine_hut','wilderness_hut') "
            "OR \"railway\" IN ('station','halt') OR \"natural\" IN ('saddle','spring','cave_entrance') "
            "OR \"waterway\" = 'waterfall' OR \"man_made\" = 'survey_point' "
            "OR \"historic\" IN ('ruins','castle','fort')) "
            "AND \"name\" IS NOT NULL"
        )
        label_root.appendChild(r_local)

    # 6. Minor POI names at HIGH label density
    if carto.label_density == DensityLevel.HIGH and carto.show_buildings:
        pal_poi = QgsPalLayerSettings()
        pal_poi.fieldName = "name"
        pal_poi.isExpression = False
        pal_poi.priority = 4
        _configure_point_pal(pal_poi, over_point=False, dist_mm=1.6)
        pal_poi.setFormat(
            _make_text_format(5.6 + fd, _pal(color_mode, "ink_soft"), halo_hex=halo_hex, halo_mm=0.55)
        )
        r_poi = QgsRuleBasedLabeling.Rule(pal_poi)
        r_poi.setDescription("POI & Facility Names")
        r_poi.setFilterExpression(
            "\"amenity\" IN ('hospital','fire_station','police','school','shelter','fuel') "
            "AND \"name\" IS NOT NULL"
        )
        label_root.appendChild(r_poi)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_us_towers_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style FAA DOF / FCC ASR tall obstacle towers (``us_towers``)."""
    from qgis.core import (
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    c_hex = _pal(color_mode, "accent_blue")

    sym = _svg_marker_symbol("obstacle_tower", 3.0 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    if units == Units.IMPERIAL:
        lbl_expr = (
            "CASE "
            "WHEN \"agl_ft\" IS NOT NULL AND to_real(\"agl_ft\") > 0 "
            "THEN to_int(round(to_real(\"agl_ft\"))) || ' ft AGL' "
            "WHEN \"amsl_ft\" IS NOT NULL AND to_real(\"amsl_ft\") > 0 "
            "THEN to_int(round(to_real(\"amsl_ft\"))) || ' ft MSL' "
            "WHEN \"name\" IS NOT NULL THEN \"name\" "
            "ELSE '' END"
        )
    else:
        lbl_expr = (
            "CASE "
            "WHEN \"agl_ft\" IS NOT NULL AND to_real(\"agl_ft\") > 0 "
            "THEN to_int(round(to_real(\"agl_ft\") * 0.3048)) || ' m AGL' "
            "WHEN \"amsl_ft\" IS NOT NULL AND to_real(\"amsl_ft\") > 0 "
            "THEN to_int(round(to_real(\"amsl_ft\") * 0.3048)) || ' m MSL' "
            "WHEN \"name\" IS NOT NULL THEN \"name\" "
            "ELSE '' END"
        )

    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 7
    _configure_point_pal(pal, over_point=False, dist_mm=1.7)
    pal.setFormat(_make_text_format(5.8 + fd, _pal(color_mode, "accent_blue_dark"), bold=True, halo_hex="#FFFFFF", halo_mm=0.65))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_enc_navaids_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style NOAA ENC buoys, beacons, and lights (``enc_navaids``)."""
    from qgis.core import (
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    c_hex = _pal(color_mode, "accent_blue")

    sym = _svg_marker_symbol("navaid", 2.6 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    pal = QgsPalLayerSettings()
    pal.fieldName = "objnam"
    pal.isExpression = False
    pal.priority = 6
    _configure_point_pal(pal, over_point=False, dist_mm=1.5)
    pal.setFormat(_make_text_format(5.8 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.6))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_mgrs_grid_1km_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsSingleSymbolRenderer
    from qgis.PyQt.QtGui import QPainter

    w = 0.20 if color_mode in (ColorMode.BW, ColorMode.NIGHT_RED) else 0.18
    sym = _line_symbol(_pal(color_mode, "grid_1km"), w, cap_round=False)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    layer.setOpacity(1.0)
    layer.setLabelsEnabled(False)


def _build_mgrs_grid_100km_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsSingleSymbolRenderer
    from qgis.PyQt.QtGui import QPainter

    w = 0.50 if _is_bw(color_mode) else (0.48 if color_mode == ColorMode.NIGHT_RED else 0.45)
    sym = _line_symbol(_pal(color_mode, "grid_100km"), w, cap_round=False)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    layer.setOpacity(1.0)
    layer.setLabelsEnabled(False)


def _build_mgrs_zone_boundary_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import QgsSingleSymbolRenderer

    w = 0.60 if _is_bw(color_mode) else 0.55
    sym = _line_symbol(_pal(color_mode, "zone_boundary"), w, dash_vector=[3.0, 1.4], cap_round=False)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_mgrs_square_labels_style(layer: Any, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0,0", "outline_style": "no", "size": "0.01"})
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    pal = QgsPalLayerSettings()
    pal.fieldName = "square_id"
    pal.isExpression = False
    pal.priority = 2
    _configure_point_pal(pal, over_point=True)
    pal.setFormat(
        _make_text_format(
            11.0,
            _pal(color_mode, "square_label"),
            bold=True,
            halo_hex=_pal(color_mode, "halo"),
            halo_mm=1.0,
            halo_opacity=0.85,
            opacity=0.68,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_overlay_style(layer: Any, role: str, color_mode: ColorMode) -> None:
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    bw = _is_bw(color_mode)
    accent = _pal(color_mode, "overlay_accent")

    if role == "overlay_polygons":
        sym = _simple_fill_symbol(
            accent if not bw else "transparent",
            stroke_hex=accent,
            stroke_mm=0.45 if not bw else 0.55,
            stroke_style="dash",
            fill_opacity=0.14 if not bw else 1.0,
        )
    elif role == "overlay_lines":
        sym = _line_symbol(accent, 0.55 if not bw else 0.65, dash_vector=[2.5, 1.1])
    else:
        sym = QgsMarkerSymbol.createSimple(
            {
                "name": "diamond",
                "color": accent if not bw else "#FFFFFF",
                "outline_color": "#FFFFFF" if not bw else "#000000",
                "outline_width": "0.35" if not bw else "0.50",
                "outline_width_unit": "MM",
                "size": "2.8",
                "size_unit": "MM",
            }
        )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    field_names = {f.name().lower(): f.name() for f in layer.fields()}
    if "name" in field_names:
        pal = QgsPalLayerSettings()
        pal.fieldName = field_names["name"]
        pal.isExpression = False
        pal.priority = 9
        if role == "overlay_points":
            _configure_point_pal(pal, over_point=False, dist_mm=1.8)
        pal.setFormat(_make_text_format(6.8, accent, bold=True, halo_hex="#FFFFFF", halo_mm=0.75))
        layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        layer.setLabelsEnabled(True)
    else:
        layer.setLabelsEnabled(False)


def _park_boundary_symbol(
    stroke_hex: str,
    width_mm: float,
    dash_vector: list[float],
    *,
    band_hex: str | None = None,
    band_alpha: int = 44,
    band_width_mm: float = 0.95,
    band_offset_mm: float = 0.55,
    tint_alpha: int = 0,
) -> Any:
    """Create a crisp topo protected-area boundary (dashed perimeter + subtle inner ribbon)."""
    from qgis.core import (
        QgsFillSymbol,
        QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer,
        QgsUnitTypes,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)

    if tint_alpha > 0 and band_hex is not None:
        fill_color = QColor(band_hex)
        fill_color.setAlpha(tint_alpha)
        bg = QgsSimpleFillSymbolLayer(fill_color)
        bg.setStrokeStyle(Qt.NoPen)
        sym.appendSymbolLayer(bg)

    if band_hex is not None and band_width_mm > 0:
        band_c = QColor(band_hex)
        band_c.setAlpha(band_alpha)
        ribbon = QgsSimpleLineSymbolLayer(band_c, band_width_mm)
        ribbon.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        ribbon.setOffset(band_offset_mm)
        ribbon.setOffsetUnit(QgsUnitTypes.RenderMillimeters)
        ribbon.setPenJoinStyle(Qt.RoundJoin)
        sym.appendSymbolLayer(ribbon)

    edge = QgsSimpleLineSymbolLayer(QColor(stroke_hex), width_mm)
    edge.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    edge.setPenCapStyle(Qt.FlatCap)
    edge.setPenJoinStyle(Qt.RoundJoin)
    edge.setUseCustomDashPattern(True)
    edge.setCustomDashVector(dash_vector)
    edge.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    sym.appendSymbolLayer(edge)
    return sym


def _build_au_protected_areas_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style DCCEEW CAPAD Australian National Parks, Nature Reserves, and IPAs."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    bw = _is_bw(color_mode)
    ipa_expr = "upper(coalesce(\"type_abbr\", '')) = 'IPA' OR lower(coalesce(\"park_type\", '')) LIKE '%indigenous%'"

    root = QgsRuleBasedRenderer.Rule(None)
    if not bw:
        ipa_c = _pal(color_mode, "park_ipa_stroke")
        np_c = _pal(color_mode, "park_stroke")
        _add_rule(
            root,
            "Indigenous Protected Areas (IPA)",
            ipa_expr,
            _park_boundary_symbol(
                ipa_c,
                0.48 * wf,
                [3.0, 1.1, 0.7, 1.1],
                band_hex=ipa_c,
                band_alpha=42,
                band_width_mm=0.95 * wf,
                band_offset_mm=0.52 * wf,
                tint_alpha=12,
            ),
        )
        _add_rule(
            root,
            "National Parks & Reserves (CAPAD)",
            f"NOT ({ipa_expr})",
            _park_boundary_symbol(
                np_c,
                0.48 * wf,
                [3.4, 1.3],
                band_hex=np_c,
                band_alpha=46,
                band_width_mm=1.0 * wf,
                band_offset_mm=0.55 * wf,
                tint_alpha=14,
            ),
        )
    else:
        _add_rule(
            root,
            "Indigenous Protected Areas (IPA)",
            ipa_expr,
            _park_boundary_symbol(
                "#1A1A1A",
                0.46 * wf,
                [2.4, 1.0, 0.6, 1.0, 0.6, 1.0],
                band_hex="#666666",
                band_alpha=180,
                band_width_mm=0.14,
                band_offset_mm=0.55,
            ),
        )
        _add_rule(
            root,
            "National Parks & Reserves (CAPAD)",
            f"NOT ({ipa_expr})",
            _park_boundary_symbol(
                "#1A1A1A",
                0.48 * wf,
                [3.2, 1.1, 0.7, 1.1],
                band_hex="#555555",
                band_alpha=180,
                band_width_mm=0.14,
                band_offset_mm=0.55,
            ),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"type_abbr\" IS NOT NULL AND length(trim(\"type_abbr\")) > 0 "
        "THEN upper(trim(\"name\")) || ' (' || upper(trim(\"type_abbr\")) || ')' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN upper(trim(\"name\")) "
        "ELSE '' END"
    )
    pal.isExpression = True
    pal.priority = 4
    pal.setFormat(
        _make_text_format(
            6.6 + fd,
            _pal(color_mode, "park_label"),
            bold=True,
            halo_hex=_pal(color_mode, "halo"),
            halo_mm=0.75,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("CAPAD Protected Area Labels")
    rule.setFilterExpression("\"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_au_hydro_polys_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia Surface Hydrology polygons (lakes, waterholes, dry claypans)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    bw = _is_bw(color_mode)
    perennial_expr = "lower(coalesce(\"perenniality\", 'perennial')) = 'perennial'"

    root = QgsRuleBasedRenderer.Rule(None)
    if not bw:
        _add_rule(
            root,
            "GA Perennial Waterbody",
            perennial_expr,
            _simple_fill_symbol(
                _pal(color_mode, "water_fill"),
                stroke_hex=_pal(color_mode, "water_stroke"),
                stroke_mm=0.20 * wf,
                stroke_style="solid",
                fill_opacity=0.65,
            ),
        )
        _add_rule(
            root,
            "GA Non-Perennial / Dry Lake",
            f"NOT ({perennial_expr})",
            _hatch_fill_symbol(
                _pal(color_mode, "au_dry_hatch"),
                angle_deg=0.0,
                spacing_mm=1.8,
                line_mm=0.12,
                bg_hex=_pal(color_mode, "au_dry_bg"),
                bg_alpha=110,
                outline_hex=_pal(color_mode, "water_stroke"),
                outline_mm=0.18 * wf,
                outline_dash=[1.8, 1.0],
                dash_vector=[1.6, 1.2],
            ),
        )
    else:
        _add_rule(
            root,
            "GA Perennial Waterbody",
            perennial_expr,
            _hatch_fill_symbol(
                "#6B6B6B",
                angle_deg=0.0,
                spacing_mm=1.3,
                line_mm=0.12,
                bg_hex="#FFFFFF",
                outline_hex="#1A1A1A",
                outline_mm=0.22 * wf,
            ),
        )
        _add_rule(
            root,
            "GA Non-Perennial / Dry Lake",
            f"NOT ({perennial_expr})",
            _hatch_fill_symbol(
                "#777777",
                angle_deg=0.0,
                spacing_mm=1.9,
                line_mm=0.12,
                outline_hex="#333333",
                outline_mm=0.18 * wf,
                outline_dash=[1.8, 1.0],
                dash_vector=[1.4, 1.4],
            ),
        )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 5
    pal.setFormat(
        _make_text_format(
            6.2 + fd,
            _pal(color_mode, "water_label"),
            italic=True,
            halo_hex="#FFFFFF",
            halo_mm=0.6,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("GA Waterbody Names")
    rule.setFilterExpression("\"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(carto.label_density != DensityLevel.LOW)


def _build_au_hydro_lines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia Surface Hydrology watercourses (perennial & non-perennial)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    arrow_iv = 34.0 / wf

    major_c = _pal(color_mode, "au_hydro_major")
    minor_c = _pal(color_mode, "au_hydro_minor")
    nonper_c = _pal(color_mode, "au_hydro_nonper")
    halo_c = _pal(color_mode, "halo")

    root = QgsRuleBasedRenderer.Rule(None)
    _add_rule(
        root,
        "GA Perennial Major Watercourse",
        "lower(coalesce(\"perenniality\", '')) = 'perennial' AND lower(coalesce(\"hierarchy\", '')) = 'major'",
        _waterway_symbol(
            major_c,
            0.46 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv,
            arrow_size_mm=1.9 * wf,
            arrow_color_hex=major_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "GA Perennial Minor Watercourse",
        "lower(coalesce(\"perenniality\", '')) = 'perennial' AND lower(coalesce(\"hierarchy\", '')) != 'major'",
        _waterway_symbol(
            minor_c,
            0.25 * wf,
            show_arrows=carto.show_river_arrows,
            arrow_interval_mm=arrow_iv * 1.2,
            arrow_size_mm=1.55 * wf,
            arrow_color_hex=major_c,
            icons_root=icons_root,
        ),
    )
    _add_rule(
        root,
        "GA Non-Perennial / Ephemeral Creek",
        "lower(coalesce(\"perenniality\", '')) != 'perennial'",
        _line_symbol(nonper_c, 0.21 * wf, dash_vector=[2.5, 0.9, 0.5, 0.9]),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=72.0 * rep_mult, min_feature_mm=15.0 * min_mult)
    pal.setFormat(_make_text_format(5.8 + fd, major_c, italic=True, halo_hex=halo_c, halo_mm=0.55))
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("GA Watercourse Names")
    rule.setFilterExpression("\"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_au_hydro_points_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia Outback waterpoints (bores, springs, waterholes, tanks, dams)."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    c_hex = _pal(color_mode, "au_bore")

    sym = _svg_marker_symbol("water_bore", 2.6 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"featuretype\" IS NOT NULL AND length(trim(\"featuretype\")) > 0 "
        "AND strpos(lower(\"name\"), lower(trim(\"featuretype\"))) = 0 "
        "THEN trim(\"name\") || ' (' || trim(\"featuretype\") || ')' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "WHEN \"featuretype\" IS NOT NULL AND length(trim(\"featuretype\")) > 0 THEN trim(\"featuretype\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 7
    _configure_point_pal(pal, over_point=False, dist_mm=1.6)
    pal.setFormat(
        _make_text_format(
            5.8 + fd,
            _pal(color_mode, "au_bore_label"),
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.65,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_power_lines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia National Electricity Transmission Lines."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    c_hex = _pal(color_mode, "au_power")

    layer.setRenderer(QgsSingleSymbolRenderer(_powerline_symbol(c_hex, render_pass=2, wf=wf * 1.15)))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"capacity_kv\" IS NOT NULL AND to_real(\"capacity_kv\") > 0 "
        "THEN to_int(round(to_real(\"capacity_kv\"))) || ' kV' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=80.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    pal.setFormat(_make_text_format(5.5 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.55))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_substations_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia electricity substations."""
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsSingleSymbolRenderer,
        QgsVectorLayerSimpleLabeling,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    c_hex = _pal(color_mode, "au_sub")

    sym = QgsMarkerSymbol.createSimple(
        {
            "name": "square",
            "color": "#FFFFFF",
            "outline_color": c_hex,
            "outline_width": f"{0.38 * wf:.3f}",
            "outline_width_unit": "MM",
            "size": f"{2.2 * wf:.3f}",
            "size_unit": "MM",
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"voltage_kv\" IS NOT NULL AND to_real(\"voltage_kv\") > 0 "
        "THEN trim(\"name\") || ' (' || to_int(round(to_real(\"voltage_kv\"))) || ' kV)' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 6
    _configure_point_pal(pal, over_point=False, dist_mm=1.5)
    pal.setFormat(_make_text_format(5.6 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.6))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_pipelines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style Geoscience Australia Oil & Gas Pipelines."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    c_hex = _pal(color_mode, "pipeline")

    layer.setRenderer(QgsSingleSymbolRenderer(_pipeline_symbol(c_hex, render_pass=2, wf=wf * 1.05)))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    lbl_expr = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "WHEN \"pipeline_type\" IS NOT NULL AND length(trim(\"pipeline_type\")) > 0 "
        "THEN upper(trim(\"pipeline_type\")) || ' PIPELINE' "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 4
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=85.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    pal.setFormat(_make_text_format(5.5 + fd, c_hex, bold=True, halo_hex="#FFFFFF", halo_mm=0.55))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_runways_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style Geoscience Australia regional aircraft runways."""
    from qgis.core import QgsSingleSymbolRenderer

    wf = scale_width_factor(scale)
    sym = _cased_road_symbol(
        _pal(color_mode, "rail"),
        1.55 * wf,
        _pal(color_mode, "runway_fill"),
        1.10 * wf,
        casing_pass=9,
        fill_pass=21,
        centerline_hex="#FFFFFF",
        centerline_mm=0.18 * wf,
        centerline_dash=[1.6, 1.1],
        centerline_pass=27,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_au_airfields_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia regional airfields & ALAs."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    c_hex = _pal(color_mode, "accent_blue")

    sym = _svg_marker_symbol("airfield", 3.0 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 8
    _configure_point_pal(pal, over_point=False, dist_mm=1.8)
    pal.setFormat(_make_text_format(6.2 + fd, _pal(color_mode, "accent_blue_dark"), bold=True, halo_hex="#FFFFFF", halo_mm=0.7))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_au_homesteads_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style Geoscience Australia Outback homesteads & pastoral stations."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    c_hex = _pal(color_mode, "ink")

    sym = _svg_marker_symbol("homestead", 2.7 * wf, fill_hex=c_hex, outline_hex=c_hex, icons_root=icons_root)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.isExpression = False
    pal.priority = 7
    _configure_point_pal(pal, over_point=False, dist_mm=1.7)
    pal.setFormat(_make_text_format(6.1 + fd, c_hex, bold=True, halo_hex=_pal(color_mode, "halo"), halo_mm=0.65))
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _halo_dashed_line_symbol(
    stroke_hex: str,
    width_mm: float,
    dash_vector: list[float],
    *,
    halo_hex: str = "#FFFFFF",
    halo_width_mm: float = 0.0,
    halo_alpha: int = 215,
    render_pass: int = 0,
) -> Any:
    """Build a dashed line symbol optionally backed by a subtle contrast halo."""
    from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer, QgsUnitTypes
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor

    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)

    if halo_width_mm > 0:
        hc = QColor(halo_hex)
        hc.setAlpha(halo_alpha)
        halo = QgsSimpleLineSymbolLayer(hc, halo_width_mm)
        halo.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        halo.setPenCapStyle(Qt.RoundCap)
        halo.setPenJoinStyle(Qt.RoundJoin)
        halo.setRenderingPass(max(0, render_pass - 1))
        sym.appendSymbolLayer(halo)

    dash = QgsSimpleLineSymbolLayer(QColor(stroke_hex), width_mm)
    dash.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    dash.setPenCapStyle(Qt.FlatCap)
    dash.setPenJoinStyle(Qt.RoundJoin)
    dash.setUseCustomDashPattern(True)
    dash.setCustomDashVector(dash_vector)
    dash.setCustomDashPatternUnit(QgsUnitTypes.RenderMillimeters)
    dash.setRenderingPass(render_pass)
    sym.appendSymbolLayer(dash)
    return sym


def _build_au_state_trails_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style AU State, NZ DOC, USFS/NPS, and CA CanVec Fire Trails, 4WD Tracks, and Walking Tracks."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    rep_mult, min_mult = _density_params(carto.label_density)
    bw = _is_bw(color_mode)
    halo_c = _pal(color_mode, "halo")

    fire_filter = "lower(coalesce(\"trail_class\", '')) IN ('fire_trail','management_trail')"
    fourwd_filter = "lower(coalesce(\"trail_class\", '')) = '4wd_track'"
    walk_filter = f"NOT ({fire_filter}) AND NOT ({fourwd_filter})"

    root = QgsRuleBasedRenderer.Rule(None)
    if not bw:
        _add_rule(
            root,
            "Fire & Management Trails",
            fire_filter,
            _halo_dashed_line_symbol(
                _pal(color_mode, "state_fire_trail"),
                0.56 * wf,
                [3.0, 1.1],
                halo_hex=halo_c,
                halo_width_mm=0.90 * wf,
                halo_alpha=200,
                render_pass=14,
            ),
        )
        _add_rule(
            root,
            "4WD Tracks",
            fourwd_filter,
            _line_symbol(_pal(color_mode, "state_4wd"), 0.42 * wf, dash_vector=[2.4, 1.0], cap_round=False, render_pass=10),
        )
        _add_rule(
            root,
            "Walking Tracks",
            walk_filter,
            _line_symbol(_pal(color_mode, "state_walk"), 0.30 * wf, dash_vector=[1.4, 0.9], cap_round=False, render_pass=8),
        )
    else:
        _add_rule(
            root,
            "Fire & Management Trails",
            fire_filter,
            _halo_dashed_line_symbol(
                "#000000",
                0.55 * wf,
                [3.2, 0.9, 1.0, 0.9],
                halo_hex="#FFFFFF",
                halo_width_mm=0.92 * wf,
                halo_alpha=235,
                render_pass=14,
            ),
        )
        _add_rule(
            root,
            "4WD Tracks",
            fourwd_filter,
            _line_symbol("#1A1A1A", 0.40 * wf, dash_vector=[2.4, 1.0], cap_round=False, render_pass=10),
        )
        _add_rule(
            root,
            "Walking Tracks",
            walk_filter,
            _line_symbol("#1A1A1A", 0.28 * wf, dash_vector=[1.0, 0.9], cap_round=False, render_pass=8),
        )

    renderer = QgsRuleBasedRenderer(root)
    renderer.setUsingSymbolLevels(True)
    layer.setRenderer(renderer)

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)

    ft_pal = QgsPalLayerSettings()
    ft_pal.fieldName = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND lower(coalesce(\"trail_class\", '')) = 'fire_trail' "
        "AND strpos(lower(\"name\"), 'fire') = 0 "
        "AND strpos(lower(\"name\"), 'trail') = 0 "
        "AND strpos(lower(\"name\"), ' ft') = 0 "
        "THEN trim(\"name\") || ' (FT)' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        "ELSE '' END"
    )
    ft_pal.isExpression = True
    ft_pal.priority = 7
    _configure_curved_line_pal(ft_pal, on_line=False, repeat_mm=65.0 * rep_mult, min_feature_mm=16.0 * min_mult)
    ft_pal.setFormat(
        _make_text_format(
            5.9 + fd,
            _pal(color_mode, "state_fire_label"),
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.68,
        )
    )
    ft_rule = QgsRuleBasedLabeling.Rule(ft_pal)
    ft_rule.setDescription("Fire Trail Labels")
    ft_rule.setFilterExpression(f"({fire_filter}) AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(ft_rule)

    trk_pal = QgsPalLayerSettings()
    trk_pal.fieldName = "name"
    trk_pal.isExpression = False
    trk_pal.priority = 5
    _configure_curved_line_pal(trk_pal, on_line=False, repeat_mm=75.0 * rep_mult, min_feature_mm=18.0 * min_mult)
    trk_pal.setFormat(
        _make_text_format(
            5.5 + fd,
            _pal(color_mode, "state_walk_label"),
            italic=False,
            halo_hex=halo_c,
            halo_mm=0.58,
        )
    )
    trk_rule = QgsRuleBasedLabeling.Rule(trk_pal)
    trk_rule.setDescription("Walking & 4WD Track Labels")
    trk_rule.setFilterExpression(f"NOT ({fire_filter}) AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(trk_rule)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_au_state_pois_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
    icons_root: Path | None = None,
) -> None:
    """Style AU State, NZ DOC, USFS/NPS, and CA CanVec Huts, Campgrounds, Trigs, Lookouts, and Sites."""
    from qgis.core import (
        QgsMarkerSymbol,
        QgsPalLayerSettings,
        QgsRuleBasedLabeling,
        QgsRuleBasedRenderer,
    )

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    ink = _pal(color_mode, "ink")
    hut_c = _pal(color_mode, "poi_hut")
    camp_c = _pal(color_mode, "poi_camp")
    lookout_c = _pal(color_mode, "poi_lookout")

    root = QgsRuleBasedRenderer.Rule(None)
    _add_rule(
        root,
        "Alpine Huts & Refuges",
        "lower(coalesce(\"poi_type\", '')) = 'hut'",
        _svg_marker_symbol("hut", 3.1 * wf, fill_hex=hut_c, outline_hex=ink, icons_root=icons_root),
    )
    _add_rule(
        root,
        "Campgrounds",
        "lower(coalesce(\"poi_type\", '')) = 'campground'",
        _svg_marker_symbol("campground", 3.0 * wf, fill_hex=camp_c, outline_hex=ink, icons_root=icons_root),
    )
    _add_rule(
        root,
        "Survey Trig Stations",
        "lower(coalesce(\"poi_type\", '')) = 'trig'",
        _svg_marker_symbol("trig", 2.9 * wf, fill_hex=ink, outline_hex=ink, icons_root=icons_root),
    )
    _add_rule(
        root,
        "Lookouts",
        "lower(coalesce(\"poi_type\", '')) = 'lookout'",
        QgsMarkerSymbol.createSimple(
            {
                "name": "diamond",
                "color": lookout_c,
                "outline_color": "#FFFFFF",
                "outline_width": f"{0.32 * wf:.3f}",
                "outline_width_unit": "MM",
                "size": f"{2.3 * wf:.3f}",
                "size_unit": "MM",
            }
        ),
    )
    _add_rule(
        root,
        "Picnic & Rec Sites",
        "lower(coalesce(\"poi_type\", '')) NOT IN ('hut','campground','trig','lookout')",
        QgsMarkerSymbol.createSimple(
            {
                "name": "circle",
                "color": camp_c,
                "outline_color": "#FFFFFF",
                "outline_width": f"{0.30 * wf:.3f}",
                "outline_width_unit": "MM",
                "size": f"{2.0 * wf:.3f}",
                "size_unit": "MM",
            }
        ),
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))

    label_root = QgsRuleBasedLabeling.Rule(None)

    if units == Units.IMPERIAL:
        elev_fmt = "to_int(round(to_real(\"elev_m\") * 3.28084)) || ' ft'"
    else:
        elev_fmt = "to_int(round(to_real(\"elev_m\"))) || ' m'"

    trig_pal = QgsPalLayerSettings()
    trig_pal.fieldName = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"elev_m\" IS NOT NULL AND to_real(\"elev_m\") > -500 "
        "AND regexp_match(coalesce(\"name\", ''), '(?i)[0-9]{2,4}\\\\s*(?:m|ft)\\\\b') = 0 "
        f"THEN trim(\"name\") || '\\n' || {elev_fmt} "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN trim(\"name\") "
        f"WHEN \"elev_m\" IS NOT NULL AND to_real(\"elev_m\") > -500 THEN {elev_fmt} "
        "ELSE '' END"
    )
    trig_pal.isExpression = True
    trig_pal.priority = 8
    _configure_point_pal(trig_pal, over_point=False, dist_mm=1.8)
    trig_pal.setFormat(_make_text_format(5.9 + fd, ink, bold=True, halo_hex="#FFFFFF", halo_mm=0.7))
    trig_rule = QgsRuleBasedLabeling.Rule(trig_pal)
    trig_rule.setDescription("Trig Station Labels")
    trig_rule.setFilterExpression("lower(coalesce(\"poi_type\", '')) = 'trig'")
    label_root.appendChild(trig_rule)

    hc_pal = QgsPalLayerSettings()
    hc_pal.fieldName = "name"
    hc_pal.isExpression = False
    hc_pal.priority = 8
    _configure_point_pal(hc_pal, over_point=False, dist_mm=1.8)
    hc_pal.setFormat(
        _make_text_format(
            6.0 + fd,
            _pal(color_mode, "poi_hc_label"),
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.72,
        )
    )
    hc_rule = QgsRuleBasedLabeling.Rule(hc_pal)
    hc_rule.setDescription("Hut & Campground Labels")
    hc_rule.setFilterExpression(
        "lower(coalesce(\"poi_type\", '')) IN ('hut','campground') "
        "AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0"
    )
    label_root.appendChild(hc_rule)

    if carto.label_density != DensityLevel.LOW:
        rec_pal = QgsPalLayerSettings()
        rec_pal.fieldName = "name"
        rec_pal.isExpression = False
        rec_pal.priority = 6
        _configure_point_pal(rec_pal, over_point=False, dist_mm=1.6)
        rec_pal.setFormat(
            _make_text_format(
                5.5 + fd,
                lookout_c if not _is_bw(color_mode) else "#1A1A1A",
                italic=True,
                halo_hex="#FFFFFF",
                halo_mm=0.6,
            )
        )
        rec_rule = QgsRuleBasedLabeling.Rule(rec_pal)
        rec_rule.setDescription("Lookout & Picnic Labels")
        rec_rule.setFilterExpression(
            "lower(coalesce(\"poi_type\", '')) NOT IN ('hut','campground','trig') "
            "AND \"name\" IS NOT NULL AND length(trim(\"name\")) > 0"
        )
        label_root.appendChild(rec_rule)

    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _waypoint_badge_symbol(
    shape: str,
    fill_hex: str,
    border_hex: str,
    size_mm: float,
) -> Any:
    """Build a high-contrast double-ring tactical waypoint marker."""
    from qgis.core import QgsMarkerSymbol, QgsSimpleMarkerSymbolLayer

    sym = QgsMarkerSymbol()
    sym.deleteSymbolLayer(0)

    outer = QgsSimpleMarkerSymbolLayer.create(
        {
            "name": shape,
            "color": "#FFFFFF",
            "outline_color": border_hex,
            "outline_width": "0.32",
            "outline_width_unit": "MM",
            "size": f"{size_mm + 0.95:.3f}",
            "size_unit": "MM",
        }
    )
    inner = QgsSimpleMarkerSymbolLayer.create(
        {
            "name": shape,
            "color": fill_hex,
            "outline_color": "#FFFFFF",
            "outline_width": "0.24",
            "outline_width_unit": "MM",
            "size": f"{size_mm:.3f}",
            "size_unit": "MM",
        }
    )
    sym.appendSymbolLayer(outer)
    sym.appendSymbolLayer(inner)
    return sym


def _build_waypoint_route_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style tactical waypoint connecting route legs across all 4 cartographic themes."""
    from qgis.core import QgsSingleSymbolRenderer

    wf = scale_width_factor(scale)
    sym = _halo_dashed_line_symbol(
        _pal(color_mode, "wp_route"),
        0.55 * wf,
        [3.2, 1.4],
        halo_hex="#FFFFFF",
        halo_width_mm=1.0 * wf,
        halo_alpha=220,
        render_pass=30,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_waypoint_points_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style numbered tactical waypoints (WP1..WPn) with category badges and top-priority callouts."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    bw = _is_bw(color_mode)
    sz = 2.6 * wf

    root = QgsRuleBasedRenderer.Rule(None)
    if not bw:
        _add_rule(
            root,
            "Camp Waypoint",
            "lower(coalesce(\"category\", '')) = 'camp'",
            _waypoint_badge_symbol("circle", _pal(color_mode, "wp_camp"), _pal(color_mode, "wp_camp_border"), sz),
        )
        _add_rule(
            root,
            "Water Waypoint",
            "lower(coalesce(\"category\", '')) = 'water'",
            _waypoint_badge_symbol("circle", _pal(color_mode, "wp_water"), _pal(color_mode, "wp_water_border"), sz),
        )
        _add_rule(
            root,
            "Hazard Waypoint",
            "lower(coalesce(\"category\", '')) = 'hazard'",
            _waypoint_badge_symbol("diamond", _pal(color_mode, "wp_hazard"), _pal(color_mode, "wp_hazard_border"), sz + 0.3),
        )
        _add_rule(
            root,
            "LZ Waypoint",
            "lower(coalesce(\"category\", '')) = 'lz'",
            _waypoint_badge_symbol("pentagon", _pal(color_mode, "wp_lz"), _pal(color_mode, "wp_lz_border"), sz + 0.2),
        )
        _add_rule(
            root,
            "Standard Waypoint",
            "lower(coalesce(\"category\", '')) NOT IN ('camp','water','hazard','lz')",
            _waypoint_badge_symbol("circle", _pal(color_mode, "wp_std"), _pal(color_mode, "wp_std_border"), sz),
        )
    else:
        _add_rule(
            root,
            "Camp Waypoint",
            "lower(coalesce(\"category\", '')) = 'camp'",
            _waypoint_badge_symbol("square", "#111111", "#000000", sz),
        )
        _add_rule(
            root,
            "Water Waypoint",
            "lower(coalesce(\"category\", '')) = 'water'",
            _waypoint_badge_symbol("pentagon", "#111111", "#000000", sz + 0.15),
        )
        _add_rule(
            root,
            "Hazard Waypoint",
            "lower(coalesce(\"category\", '')) = 'hazard'",
            _waypoint_badge_symbol("diamond", "#000000", "#000000", sz + 0.35),
        )
        _add_rule(
            root,
            "LZ Waypoint",
            "lower(coalesce(\"category\", '')) = 'lz'",
            _waypoint_badge_symbol("triangle", "#000000", "#000000", sz + 0.25),
        )
        _add_rule(
            root,
            "Standard Waypoint",
            "lower(coalesce(\"category\", '')) NOT IN ('camp','water','hazard','lz')",
            _waypoint_badge_symbol("circle", "#111111", "#000000", sz),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))

    lbl_expr = (
        "CASE "
        "WHEN \"code\" IS NOT NULL AND length(trim(\"code\")) > 0 "
        "AND \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 "
        "AND lower(trim(\"label\")) != lower(trim(\"code\")) "
        "THEN trim(\"code\") || ' · ' || trim(\"label\") "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 THEN trim(\"label\") "
        "WHEN \"code\" IS NOT NULL AND length(trim(\"code\")) > 0 THEN trim(\"code\") "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 10
    _configure_point_pal(pal, over_point=False, dist_mm=2.2)
    pal.setFormat(
        _make_text_format(
            6.5 + fd,
            _pal(color_mode, "wp_label"),
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.85,
            halo_opacity=0.98,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_viewshed_rings_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style RF / LOS viewshed max-range boundary rings."""
    from qgis.core import QgsSingleSymbolRenderer

    wf = scale_width_factor(scale)
    sym = _halo_dashed_line_symbol(
        _pal(color_mode, "viewshed_ring"),
        0.38 * wf,
        [2.6, 1.4],
        halo_hex="#FFFFFF",
        halo_width_mm=0.72 * wf,
        halo_alpha=185,
        render_pass=28,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.setLabelsEnabled(False)


def _build_viewshed_observers_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    icons_root: Path | None = None,
) -> None:
    """Style RF / LOS observer beacon pins with AGL antenna height callouts."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    fill_c = _pal(color_mode, "viewshed_obs")
    outline_c = _pal(color_mode, "viewshed_obs_out")

    sym = _svg_marker_symbol(
        "observer",
        3.3 * wf,
        fill_hex=fill_c,
        outline_hex=outline_c,
        icons_root=icons_root,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    if units == Units.IMPERIAL:
        h_fmt = "to_int(round(to_real(\"observer_height_m\") * 3.28084)) || ' ft AGL'"
    else:
        h_fmt = (
            "CASE WHEN abs(to_real(\"observer_height_m\") - round(to_real(\"observer_height_m\"))) < 0.05 "
            "THEN to_int(round(to_real(\"observer_height_m\"))) "
            "ELSE round(to_real(\"observer_height_m\"), 1) END || ' m AGL'"
        )

    lbl_expr = (
        "CASE "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 AND \"observer_height_m\" IS NOT NULL "
        f"THEN trim(\"label\") || ' (' || {h_fmt} || ')' "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 THEN trim(\"label\") "
        f"WHEN \"observer_height_m\" IS NOT NULL THEN 'Observer (' || {h_fmt} || ')' "
        "ELSE 'Observer' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 10
    _configure_point_pal(pal, over_point=False, dist_mm=2.2)
    pal.setFormat(
        _make_text_format(
            6.3 + fd,
            outline_c,
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.85,
            halo_opacity=0.98,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_public_lands_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
    carto: CartoOptions = _DEFAULT_CARTO,
) -> None:
    """Style US PAD-US/USFS/NPS/BLM & Canada Parks/Protected Areas (``public_lands``)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    bw = _is_bw(color_mode)

    np_expr = (
        "lower(coalesce(\"designation\", '')) LIKE '%wilderness%' "
        "OR lower(coalesce(\"designation\", '')) LIKE '%national park%' "
        "OR upper(coalesce(\"manager\", '')) IN ('NPS','PC','PARKS CANADA')"
    )
    nf_expr = (
        f"NOT ({np_expr}) AND ("
        "upper(coalesce(\"manager\", '')) IN ('USFS','FS') "
        "OR lower(coalesce(\"designation\", '')) LIKE '%forest%'"
        ")"
    )
    blm_expr = f"NOT ({np_expr}) AND NOT ({nf_expr})"

    root = QgsRuleBasedRenderer.Rule(None)
    if not bw:
        np_c = _pal(color_mode, "public_np_stroke")
        nf_c = _pal(color_mode, "public_nf_stroke")
        blm_c = _pal(color_mode, "public_blm_stroke")
        _add_rule(
            root,
            "National Parks & Wilderness",
            np_expr,
            _park_boundary_symbol(
                np_c,
                0.50 * wf,
                [3.4, 1.2],
                band_hex=np_c,
                band_alpha=46,
                band_width_mm=1.05 * wf,
                band_offset_mm=0.55 * wf,
                tint_alpha=12,
            ),
        )
        _add_rule(
            root,
            "National & Crown Forests",
            nf_expr,
            _park_boundary_symbol(
                nf_c,
                0.44 * wf,
                [2.8, 1.2, 0.7, 1.2],
                band_hex=nf_c,
                band_alpha=38,
                band_width_mm=0.90 * wf,
                band_offset_mm=0.48 * wf,
                tint_alpha=10,
            ),
        )
        _add_rule(
            root,
            "BLM / Public Lands",
            blm_expr,
            _park_boundary_symbol(
                blm_c,
                0.40 * wf,
                [2.4, 1.1, 0.6, 1.1],
                band_hex=blm_c,
                band_alpha=34,
                band_width_mm=0.80 * wf,
                band_offset_mm=0.45 * wf,
                tint_alpha=10,
            ),
        )
    else:
        _add_rule(
            root,
            "National Parks & Wilderness",
            np_expr,
            _park_boundary_symbol(
                "#1A1A1A",
                0.48 * wf,
                [3.2, 1.1, 0.7, 1.1],
                band_hex="#555555",
                band_alpha=180,
                band_width_mm=0.14,
                band_offset_mm=0.55,
            ),
        )
        _add_rule(
            root,
            "National & Crown Forests",
            nf_expr,
            _park_boundary_symbol(
                "#262626",
                0.42 * wf,
                [2.6, 1.2],
                band_hex="#666666",
                band_alpha=160,
                band_width_mm=0.12,
                band_offset_mm=0.50,
            ),
        )
        _add_rule(
            root,
            "BLM / Public Lands",
            blm_expr,
            _park_boundary_symbol(
                "#333333",
                0.38 * wf,
                [1.8, 1.0, 0.5, 1.0],
                band_hex="#777777",
                band_alpha=150,
                band_width_mm=0.12,
                band_offset_mm=0.45,
            ),
        )

    layer.setRenderer(QgsRuleBasedRenderer(root))

    if carto.label_density == DensityLevel.LOW:
        layer.setLabelsEnabled(False)
        return

    label_root = QgsRuleBasedLabeling.Rule(None)
    pal = QgsPalLayerSettings()
    pal.fieldName = (
        "CASE "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 "
        "AND \"manager\" IS NOT NULL AND length(trim(\"manager\")) > 0 "
        "AND strpos(upper(\"name\"), upper(trim(\"manager\"))) = 0 "
        "THEN upper(trim(\"name\")) || ' (' || upper(trim(\"manager\")) || ')' "
        "WHEN \"name\" IS NOT NULL AND length(trim(\"name\")) > 0 THEN upper(trim(\"name\")) "
        "ELSE '' END"
    )
    pal.isExpression = True
    pal.priority = 4
    pal.setFormat(
        _make_text_format(
            6.5 + fd,
            _pal(color_mode, "public_label"),
            bold=True,
            halo_hex=_pal(color_mode, "halo"),
            halo_mm=0.75,
        )
    )
    rule = QgsRuleBasedLabeling.Rule(pal)
    rule.setDescription("Public Lands Labels")
    rule.setFilterExpression("\"name\" IS NOT NULL AND length(trim(\"name\")) > 0")
    label_root.appendChild(rule)
    layer.setLabeling(QgsRuleBasedLabeling(label_root))
    layer.setLabelsEnabled(True)


def _build_tactical_sectors_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style user-authored tactical sectors and AOI corridors (``tactical_sectors``)."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    bw = _is_bw(color_mode)
    sec_c = _pal(color_mode, "tactical_sector")

    if not bw:
        sym = _hatch_fill_symbol(
            sec_c,
            angle_deg=45.0,
            spacing_mm=3.6,
            line_mm=0.12 * wf,
            bg_hex=sec_c,
            bg_alpha=28,
            outline_hex=sec_c,
            outline_mm=0.52 * wf,
            outline_dash=[3.2, 1.2],
        )
    else:
        sym = _hatch_fill_symbol(
            "#333333",
            angle_deg=45.0,
            spacing_mm=3.2,
            line_mm=0.14 * wf,
            outline_hex="#000000",
            outline_mm=0.55 * wf,
            outline_dash=[3.2, 1.2],
        )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    lbl_expr = (
        "CASE "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 "
        "AND \"area_km2\" IS NOT NULL AND to_real(\"area_km2\") > 0 "
        "THEN upper(trim(\"label\")) || '\\n' || round(to_real(\"area_km2\"), 2) || ' km²' "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 THEN upper(trim(\"label\")) "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 9
    pal.setFormat(
        _make_text_format(
            6.3 + fd,
            sec_c,
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.80,
            halo_opacity=0.96,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_tactical_rings_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style concentric tactical range rings (``tactical_rings``) with distance callouts."""
    from qgis.core import QgsPalLayerSettings, QgsSingleSymbolRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    ring_c = _pal(color_mode, "tactical_ring")

    sym = _halo_dashed_line_symbol(
        ring_c,
        0.42 * wf,
        [2.4, 1.1],
        halo_hex="#FFFFFF",
        halo_width_mm=0.78 * wf,
        halo_alpha=200,
        render_pass=29,
    )
    layer.setRenderer(QgsSingleSymbolRenderer(sym))

    lbl_expr = (
        "CASE "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 THEN trim(\"label\") "
        "WHEN \"radius_km\" IS NOT NULL AND to_real(\"radius_km\") > 0 "
        "THEN CASE WHEN abs(to_real(\"radius_km\") - round(to_real(\"radius_km\"))) < 0.01 "
        "THEN to_int(round(to_real(\"radius_km\"))) || ' km' "
        "ELSE round(to_real(\"radius_km\"), 2) || ' km' END "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 9
    _configure_curved_line_pal(pal, on_line=True, repeat_mm=65.0, min_feature_mm=8.0)
    pal.setFormat(
        _make_text_format(
            5.8 + fd,
            ring_c,
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.75,
            halo_opacity=0.96,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _build_tactical_lines_style(
    layer: Any,
    color_mode: ColorMode,
    *,
    scale: int = 25000,
) -> None:
    """Style tactical bearing lines, pinned ruler legs, and RF LOS links (``tactical_lines``)."""
    from qgis.core import QgsPalLayerSettings, QgsRuleBasedRenderer, QgsVectorLayerSimpleLabeling

    wf = scale_width_factor(scale)
    fd = scale_font_delta(scale)
    line_c = _pal(color_mode, "tactical_line")
    rf_c = _pal(color_mode, "tactical_rf_line")
    lbl_c = _pal(color_mode, "tactical_label")

    root = QgsRuleBasedRenderer.Rule(None)
    _add_rule(
        root,
        "RF Point-to-Point Link",
        "lower(coalesce(\"kind\", '')) = 'rf_link'",
        _halo_dashed_line_symbol(
            rf_c,
            0.50 * wf,
            [3.4, 1.0, 0.8, 1.0],
            halo_hex="#FFFFFF",
            halo_width_mm=0.95 * wf,
            halo_alpha=215,
            render_pass=31,
        ),
    )
    _add_rule(
        root,
        "Bearing / Tactical Line",
        "lower(coalesce(\"kind\", '')) != 'rf_link'",
        _halo_dashed_line_symbol(
            line_c,
            0.55 * wf,
            [4.2, 1.3],
            halo_hex="#FFFFFF",
            halo_width_mm=1.0 * wf,
            halo_alpha=220,
            render_pass=31,
        ),
    )
    renderer = QgsRuleBasedRenderer(root)
    renderer.setUsingSymbolLevels(True)
    layer.setRenderer(renderer)

    lbl_expr = (
        "CASE "
        "WHEN \"label\" IS NOT NULL AND length(trim(\"label\")) > 0 THEN trim(\"label\") "
        "WHEN \"distance_km\" IS NOT NULL AND \"bearing_mag_deg\" IS NOT NULL "
        "THEN round(to_real(\"distance_km\"), 2) || ' km · ' || "
        "lpad(to_string(to_int(round(to_real(\"bearing_mag_deg\")))), 3, '0') || '°M' "
        "WHEN \"distance_km\" IS NOT NULL THEN round(to_real(\"distance_km\"), 2) || ' km' "
        "ELSE '' END"
    )
    pal = QgsPalLayerSettings()
    pal.fieldName = lbl_expr
    pal.isExpression = True
    pal.priority = 10
    _configure_curved_line_pal(pal, on_line=False, repeat_mm=80.0, min_feature_mm=8.0)
    pal.setFormat(
        _make_text_format(
            6.1 + fd,
            lbl_c,
            bold=True,
            halo_hex="#FFFFFF",
            halo_mm=0.82,
            halo_opacity=0.98,
        )
    )
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def apply_programmatic_style(
    layer: Any,
    role: str,
    color_mode: ColorMode,
    units: Units = Units.METRIC,
    *,
    scale: int = 25000,
    carto: CartoOptions | None = None,
    icons_root: Path | None = None,
) -> None:
    """Apply GridForge's programmatic PyQGIS 3.40 style for ``role``."""
    opts = carto if carto is not None else _DEFAULT_CARTO

    if role == "landcover":
        _build_landcover_style(layer, color_mode, carto=opts, icons_root=icons_root)
    elif role == "hillshade":
        _build_hillshade_style(layer, color_mode, carto=opts)
    elif role == "viewshed":
        _build_viewshed_style(layer, color_mode)
    elif role == "slope_shading":
        _build_slope_shading_style(layer, color_mode)
    elif role == "fema_flood":
        _build_fema_flood_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_protected_areas":
        _build_au_protected_areas_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "public_lands":
        _build_public_lands_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "water_polygons":
        _build_water_polygons_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_hydro_polys":
        _build_au_hydro_polys_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "contours":
        _build_contours_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "enc_depth_contours":
        _build_enc_depth_contours_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "nhd_flowlines":
        _build_nhd_flowlines_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_hydro_lines":
        _build_au_hydro_lines_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "waterways":
        _build_waterways_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "buildings":
        _build_buildings_style(layer, color_mode, carto=opts)
    elif role == "building_icons":
        _build_building_icons_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "transport":
        _build_transport_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_state_trails":
        _build_au_state_trails_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_power_lines":
        _build_au_power_lines_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_pipelines":
        _build_au_pipelines_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_runways":
        _build_au_runways_style(layer, color_mode, scale=scale)
    elif role == "points":
        _build_points_style(layer, color_mode, units=units, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_state_pois":
        _build_au_state_pois_style(layer, color_mode, units=units, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_substations":
        _build_au_substations_style(layer, color_mode, scale=scale, carto=opts)
    elif role == "au_hydro_points":
        _build_au_hydro_points_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_homesteads":
        _build_au_homesteads_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "au_airfields":
        _build_au_airfields_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "us_towers":
        _build_us_towers_style(layer, color_mode, units=units, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "enc_navaids":
        _build_enc_navaids_style(layer, color_mode, scale=scale, carto=opts, icons_root=icons_root)
    elif role == "mgrs_grid_1km":
        _build_mgrs_grid_1km_style(layer, color_mode)
    elif role == "mgrs_grid_100km":
        _build_mgrs_grid_100km_style(layer, color_mode)
    elif role == "mgrs_zone_boundary":
        _build_mgrs_zone_boundary_style(layer, color_mode)
    elif role == "mgrs_square_labels":
        _build_mgrs_square_labels_style(layer, color_mode)
    elif role in ("overlay_polygons", "overlay_lines", "overlay_points"):
        _build_overlay_style(layer, role, color_mode)
    elif role == "tactical_sectors":
        _build_tactical_sectors_style(layer, color_mode, scale=scale)
    elif role == "tactical_rings":
        _build_tactical_rings_style(layer, color_mode, scale=scale)
    elif role == "tactical_lines":
        _build_tactical_lines_style(layer, color_mode, scale=scale)
    elif role == "waypoint_route":
        _build_waypoint_route_style(layer, color_mode, scale=scale)
    elif role == "waypoint_points":
        _build_waypoint_points_style(layer, color_mode, scale=scale)
    elif role == "viewshed_rings":
        _build_viewshed_rings_style(layer, color_mode, scale=scale)
    elif role == "viewshed_observers":
        _build_viewshed_observers_style(layer, color_mode, units=units, scale=scale, icons_root=icons_root)
    else:
        raise ValueError(f"Unknown GridForge style role: {role!r}")


def _can_use_cached_qml(
    role: str,
    units: Units,
    scale: int,
    carto: CartoOptions,
) -> bool:
    """Return True when the static 1:25k default Metric ``.qml`` matches the requested options."""
    if role in ("mgrs_grid_1km", "mgrs_grid_100km", "mgrs_zone_boundary", "mgrs_square_labels"):
        return True
    if role in ("overlay_polygons", "overlay_lines", "overlay_points"):
        return True
    if scale not in (24000, 25000):
        return False
    if carto != _DEFAULT_CARTO:
        return False
    if units == Units.IMPERIAL and role in ("points", "us_towers", "au_state_pois", "viewshed_observers"):
        return False
    return True


def apply_layer_style(
    layer: Any,
    role: str,
    color_mode: ColorMode,
    *,
    units: Units = Units.METRIC,
    scale: int = 25000,
    carto: CartoOptions | None = None,
    styles_root: Path | None = None,
    icons_root: Path | None = None,
    prefer_qml: bool = True,
) -> bool:
    """Apply styling for ``role`` from versioned ``.qml`` with dynamic programmatic fallback.

    Returns ``True`` if loaded from a native QGIS 3.40 ``.qml`` file, ``False`` if applied
    programmatically. Dynamic ``CartoOptions`` overrides, non-25k multi-scale sizing, and
    Imperial unit expressions are applied programmatically so toggles never get masked by
    static QML files.
    """
    opts = carto if carto is not None else _DEFAULT_CARTO
    qml_path = get_qml_path(role, color_mode, styles_root=styles_root)
    use_qml = (
        prefer_qml
        and _can_use_cached_qml(role, units, scale, opts)
        and qml_path.is_file()
        and _NATIVE_QML_MARKER in qml_path.read_text(encoding="utf-8", errors="ignore")[:512]
    )

    if use_qml:
        try:
            msg, ok = layer.loadNamedStyle(str(qml_path))
            renderer_ok = ok and layer.renderer() is not None
            labeling_ok = (role not in _LABELED_ROLES) or (
                layer.labelsEnabled() and layer.labeling() is not None
            )
            if renderer_ok and labeling_ok:
                if role == "hillshade":
                    _build_hillshade_style(layer, color_mode, carto=opts)
                elif role == "slope_shading":
                    _build_slope_shading_style(layer, color_mode)
                elif role == "viewshed":
                    _build_viewshed_style(layer, color_mode)
                return True
            logger.warning(
                "QML style %s incomplete (ok=%s, renderer=%s, labeling=%s, msg=%s); "
                "falling back to programmatic style",
                qml_path,
                ok,
                layer.renderer() is not None,
                labeling_ok,
                msg,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning("Exception loading QML %s (%s); falling back to programmatic style", qml_path, exc)

    apply_programmatic_style(
        layer,
        role,
        color_mode,
        units=units,
        scale=scale,
        carto=opts,
        icons_root=icons_root,
    )
    return False


def _list_gpkg_layers(gpkg_path: Path) -> list[str]:
    """List vector layer names present in a GeoPackage using OGR."""
    from osgeo import ogr

    ogr.UseExceptions()
    if not gpkg_path.is_file():
        return []
    ds = ogr.Open(str(gpkg_path))
    if ds is None:
        return []
    try:
        return [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    finally:
        ds = None


_ROLE_SQLITE_SUBSET_FILTERS: dict[str, tuple[tuple[str, ...], str]] = {
    "building_icons": (
        ("amenity", "emergency", "aeroway", "building"),
        "\"amenity\" IS NOT NULL OR \"emergency\" IS NOT NULL OR \"aeroway\" = 'helipad' "
        "OR \"building\" IN ('hospital','school','church','chapel','cathedral','mosque','temple','synagogue','hut')",
    ),
    "buildings": (
        ("building", "power"),
        "(\"building\" IS NOT NULL AND \"building\" != 'no') OR \"power\" = 'substation'",
    ),
    "water_polygons": (
        ("natural", "water", "waterway", "landuse"),
        "\"natural\" = 'water' OR \"water\" IS NOT NULL OR \"waterway\" IS NOT NULL "
        "OR \"landuse\" IN ('reservoir','basin')",
    ),
    "landcover": (
        ("landuse", "natural", "leisure", "aeroway"),
        "\"landuse\" IS NOT NULL OR \"natural\" IS NOT NULL OR \"leisure\" IS NOT NULL OR \"aeroway\" IS NOT NULL",
    ),
    "waterways": (
        ("waterway",),
        "\"waterway\" IS NOT NULL",
    ),
    "transport": (
        ("highway", "railway", "aeroway", "power", "man_made", "barrier", "natural", "ford"),
        "\"highway\" IS NOT NULL OR \"railway\" IS NOT NULL OR \"aeroway\" IS NOT NULL "
        "OR \"power\" IS NOT NULL OR \"man_made\" IS NOT NULL OR \"barrier\" IS NOT NULL "
        "OR \"natural\" = 'cliff' OR \"ford\" = 'yes'",
    ),
}


def _load_vector_layer(
    project: Any,
    gpkg_path: Path,
    layer_name: str,
    display_name: str,
    *,
    role: str,
    workspace: JobWorkspace,
) -> Any | None:
    from qgis.core import QgsVectorLayer

    if not gpkg_path.is_file():
        return None
    uri = f"{gpkg_path}|layername={layer_name}"
    vlayer = QgsVectorLayer(uri, display_name, "ogr")
    if not vlayer.isValid():
        logger.debug("Skipping unavailable vector layer %s (%s)", display_name, uri)
        return None

    subset_spec = _ROLE_SQLITE_SUBSET_FILTERS.get(role)
    if subset_spec is not None:
        req_cols, subset_expr = subset_spec
        layer_fields = {vlayer.fields().at(i).name() for i in range(vlayer.fields().count())}
        if all(col in layer_fields for col in req_cols):
            vlayer.setSubsetString(subset_expr)

    spec = workspace.layout.spec
    apply_layer_style(
        vlayer,
        role,
        spec.color_mode,
        units=spec.units,
        scale=spec.scale,
        carto=spec.carto,
    )
    project.addMapLayer(vlayer, False)
    return vlayer


def _load_overlay_layers(project: Any, workspace: JobWorkspace) -> list[Any]:
    """Load optional user overlay GeoPackage layers ordered Points -> Lines -> Polygons."""
    from qgis.core import Qgis, QgsVectorLayer, QgsWkbTypes

    if workspace.overlays_gpkg is None or not workspace.overlays_gpkg.is_file():
        return []

    layer_names = _list_gpkg_layers(workspace.overlays_gpkg)
    points_layers: list[Any] = []
    lines_layers: list[Any] = []
    poly_layers: list[Any] = []
    spec = workspace.layout.spec

    for lname in layer_names:
        uri = f"{workspace.overlays_gpkg}|layername={lname}"
        vlayer = QgsVectorLayer(uri, f"overlay_{lname}", "ogr")
        if not vlayer.isValid():
            continue

        geom_type = vlayer.geometryType()
        point_enum = getattr(Qgis.GeometryType, "Point", QgsWkbTypes.PointGeometry)
        line_enum = getattr(Qgis.GeometryType, "Line", QgsWkbTypes.LineGeometry)
        poly_enum = getattr(Qgis.GeometryType, "Polygon", QgsWkbTypes.PolygonGeometry)

        if geom_type == point_enum:
            role = "overlay_points"
            target_bucket = points_layers
        elif geom_type == line_enum:
            role = "overlay_lines"
            target_bucket = lines_layers
        elif geom_type == poly_enum:
            role = "overlay_polygons"
            target_bucket = poly_layers
        else:
            continue

        apply_layer_style(
            vlayer,
            role,
            spec.color_mode,
            units=spec.units,
            scale=spec.scale,
            carto=spec.carto,
        )
        project.addMapLayer(vlayer, False)
        target_bucket.append(vlayer)

    return points_layers + lines_layers + poly_layers


def populate_project_layers(
    project: Any,
    workspace: JobWorkspace,
    *,
    include_grid: bool | None = None,
) -> list[Any]:
    """Load and style all workspace layers in exact top-to-bottom QGIS map order.

    Shared by BOTH print PDF rendering (``pyqgis_worker.py``) and Phase 3 collarless
    GeoTIFF / MBTiles / Garmin KMZ raster export (``export/raster.py``).

    Index 0 is drawn LAST (on top); last index is drawn FIRST (bottom).
    Hillshade (Multiply) sits immediately above landcover polygons so every layer
    above hillshade remains 100% crisp un-rasterized vector graphics in PDF export.
    """
    from qgis.core import QgsCoordinateReferenceSystem, QgsRasterLayer

    spec = workspace.layout.spec
    carto = spec.carto
    show_grid = carto.show_grid if include_grid is None else include_grid

    project.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{workspace.layout.utm_epsg}"))

    osm_layers = set(_list_gpkg_layers(workspace.osm_gpkg))
    mgrs_layers = set(_list_gpkg_layers(workspace.mgrs_gpkg))
    terrain_layers = set(_list_gpkg_layers(workspace.terrain_gpkg))

    regional = workspace.regional_overlays or {}
    fema_path = Path(regional["fema"]) if "fema" in regional else None
    nhd_path = Path(regional["nhd"]) if "nhd" in regional else None
    towers_path = Path(regional["towers"]) if "towers" in regional else None
    enc_path = Path(regional["enc"]) if "enc" in regional else None

    au_hydro_path = Path(regional["au-hydro"]) if "au-hydro" in regional else (
        Path(regional["au_hydro"]) if "au_hydro" in regional else None
    )
    au_parks_path = Path(regional["au-parks"]) if "au-parks" in regional else (
        Path(regional["au_parks"]) if "au_parks" in regional else None
    )
    au_infra_path = Path(regional["au-infra"]) if "au-infra" in regional else (
        Path(regional["au_infra"]) if "au_infra" in regional else None
    )
    au_aviation_path = Path(regional["au-aviation"]) if "au-aviation" in regional else (
        Path(regional["au_aviation"]) if "au_aviation" in regional else None
    )

    au_state_trail_entries: list[tuple[str, Path, set[str]]] = []
    seen_state_paths: set[Path] = set()
    unified_trail_kinds = (
        *AU_STATE_TRAIL_OVERLAY_KINDS,
        NZOverlayKind.NZ_DOC_TRAILS,
        USOverlayKind.US_TRAILS,
        CAOverlayKind.CA_CANVEC_TRAILS,
    )
    for st_kind in unified_trail_kinds:
        raw_st = regional.get(st_kind.value) or regional.get(st_kind.value.replace("-", "_"))
        if raw_st is None:
            continue
        st_path = Path(raw_st)
        resolved_st = st_path.resolve()
        if resolved_st in seen_state_paths:
            continue
        seen_state_paths.add(resolved_st)
        st_layers = set(_list_gpkg_layers(st_path))
        if st_layers:
            if st_kind.value == "au-state-trails":
                label_prefix = "AU State"
            elif st_kind.value == "nz-doc-trails":
                label_prefix = "NZ DOC"
            elif st_kind.value == "us-trails":
                label_prefix = "USFS / NPS"
            elif st_kind.value == "ca-canvec-trails":
                label_prefix = "CA CanVec"
            else:
                label_prefix = st_kind.value.split("-")[1].upper()
            au_state_trail_entries.append((label_prefix, st_path, st_layers))

    public_lands_entries: list[tuple[str, Path]] = []
    seen_pl_paths: set[Path] = set()
    for pl_key, pl_label in (
        (USOverlayKind.US_PUBLIC_LANDS.value, "US Public Lands"),
        (CAOverlayKind.CA_CANVEC_TRAILS.value, "CA Protected Areas"),
    ):
        raw_pl = regional.get(pl_key) or regional.get(pl_key.replace("-", "_"))
        if raw_pl is None:
            continue
        pl_path = Path(raw_pl)
        resolved_pl = pl_path.resolve()
        if resolved_pl in seen_pl_paths:
            continue
        if "public_lands" in set(_list_gpkg_layers(pl_path)):
            seen_pl_paths.add(resolved_pl)
            public_lands_entries.append((pl_label, pl_path))

    fema_layers = set(_list_gpkg_layers(fema_path)) if fema_path else set()
    nhd_layers = set(_list_gpkg_layers(nhd_path)) if nhd_path else set()
    towers_layers = set(_list_gpkg_layers(towers_path)) if towers_path else set()
    enc_layers = set(_list_gpkg_layers(enc_path)) if enc_path else set()
    au_hydro_layers = set(_list_gpkg_layers(au_hydro_path)) if au_hydro_path else set()
    au_parks_layers = set(_list_gpkg_layers(au_parks_path)) if au_parks_path else set()
    au_infra_layers = set(_list_gpkg_layers(au_infra_path)) if au_infra_path else set()
    au_aviation_layers = set(_list_gpkg_layers(au_aviation_path)) if au_aviation_path else set()
    waypoints_path = workspace.waypoints_gpkg if (workspace.waypoints_gpkg and workspace.waypoints_gpkg.is_file()) else None
    waypoints_layers = set(_list_gpkg_layers(waypoints_path)) if waypoints_path else set()
    vs_obs_path = (
        workspace.viewshed_observers_gpkg
        if (workspace.viewshed_observers_gpkg and workspace.viewshed_observers_gpkg.is_file())
        else None
    )
    vs_obs_layers = set(_list_gpkg_layers(vs_obs_path)) if vs_obs_path else set()
    tactical_path = (
        workspace.tactical_graphics_gpkg
        if (workspace.tactical_graphics_gpkg and workspace.tactical_graphics_gpkg.is_file())
        else None
    )
    tactical_layers = set(_list_gpkg_layers(tactical_path)) if tactical_path else set()

    ordered_top_to_bottom: list[Any] = []

    def _push(layer: Any | None) -> None:
        if layer is not None:
            ordered_top_to_bottom.append(layer)

    # 0. Tactical Custom Waypoints, RF/LOS Viewshed Observers, and Tactical Graphics on very top
    if waypoints_path:
        if "waypoint_points" in waypoints_layers:
            _push(
                _load_vector_layer(
                    project, waypoints_path, "waypoint_points", "Waypoints", role="waypoint_points", workspace=workspace
                )
            )
        if "waypoint_route" in waypoints_layers:
            _push(
                _load_vector_layer(
                    project, waypoints_path, "waypoint_route", "Waypoint Route", role="waypoint_route", workspace=workspace
                )
            )

    if vs_obs_path:
        if "viewshed_observers" in vs_obs_layers:
            _push(
                _load_vector_layer(
                    project,
                    vs_obs_path,
                    "viewshed_observers",
                    "LOS / RF Observers",
                    role="viewshed_observers",
                    workspace=workspace,
                )
            )
        if "viewshed_rings" in vs_obs_layers:
            _push(
                _load_vector_layer(
                    project,
                    vs_obs_path,
                    "viewshed_rings",
                    "LOS / RF Max Range",
                    role="viewshed_rings",
                    workspace=workspace,
                )
            )

    if tactical_path:
        if "tactical_lines" in tactical_layers:
            _push(
                _load_vector_layer(
                    project,
                    tactical_path,
                    "tactical_lines",
                    "Tactical Bearing & RF Lines",
                    role="tactical_lines",
                    workspace=workspace,
                )
            )
        if "tactical_rings" in tactical_layers:
            _push(
                _load_vector_layer(
                    project,
                    tactical_path,
                    "tactical_rings",
                    "Tactical Range Rings",
                    role="tactical_rings",
                    workspace=workspace,
                )
            )
        if "tactical_sectors" in tactical_layers:
            _push(
                _load_vector_layer(
                    project,
                    tactical_path,
                    "tactical_sectors",
                    "Tactical Sectors & AOIs",
                    role="tactical_sectors",
                    workspace=workspace,
                )
            )

    # 1. User overlays (uploaded GPX/GeoJSON routes & POIs)
    ordered_top_to_bottom.extend(_load_overlay_layers(project, workspace))

    # 2. MGRS Grid stack (100% pure vector, BlendNormal; toggleable via carto.show_grid / include_grid)
    if show_grid:
        if "square_labels" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "square_labels", "MGRS Square IDs", role="mgrs_square_labels", workspace=workspace
                )
            )
        if "zone_boundary" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "zone_boundary", "UTM Zone Boundary", role="mgrs_zone_boundary", workspace=workspace
                )
            )
        if "grid_100km" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "grid_100km", "MGRS 100 km Grid", role="mgrs_grid_100km", workspace=workspace
                )
            )
        if "grid_1km" in mgrs_layers:
            _push(
                _load_vector_layer(
                    project, workspace.mgrs_gpkg, "grid_1km", "MGRS 1 km Grid", role="mgrs_grid_1km", workspace=workspace
                )
            )

    # 3. Regional Point Overlays (US Obstacles/Navaids + AU National + Pan-AU State Huts/Camps/Trigs)
    if towers_path and "us_towers" in towers_layers:
        _push(_load_vector_layer(project, towers_path, "us_towers", "US Obstacle Towers", role="us_towers", workspace=workspace))
    if enc_path and "enc_navaids" in enc_layers:
        _push(_load_vector_layer(project, enc_path, "enc_navaids", "NOAA ENC Navaids", role="enc_navaids", workspace=workspace))
    for label_prefix, st_path, st_layers in au_state_trail_entries:
        if "au_state_pois" in st_layers:
            _push(
                _load_vector_layer(
                    project,
                    st_path,
                    "au_state_pois",
                    f"{label_prefix} Huts, Camps & Trigs",
                    role="au_state_pois",
                    workspace=workspace,
                )
            )
    if au_aviation_path and "au_airfields" in au_aviation_layers:
        _push(_load_vector_layer(project, au_aviation_path, "au_airfields", "AU Airfields", role="au_airfields", workspace=workspace))
    if au_aviation_path and "au_homesteads" in au_aviation_layers:
        _push(_load_vector_layer(project, au_aviation_path, "au_homesteads", "AU Homesteads", role="au_homesteads", workspace=workspace))
    if au_hydro_path and "au_hydro_points" in au_hydro_layers:
        _push(_load_vector_layer(project, au_hydro_path, "au_hydro_points", "AU Waterpoints & Bores", role="au_hydro_points", workspace=workspace))
    if au_infra_path and "au_substations" in au_infra_layers and carto.show_power:
        _push(_load_vector_layer(project, au_infra_path, "au_substations", "AU Substations", role="au_substations", workspace=workspace))

    # 4. OSM Points & Building-Polygon Usage Icons
    if "points" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "points", "Points & Places", role="points", workspace=workspace))
    if "multipolygons" in osm_layers:
        _push(
            _load_vector_layer(
                project, workspace.osm_gpkg, "multipolygons", "Building Usage Icons", role="building_icons", workspace=workspace
            )
        )

    # 5. OSM Transport & Infrastructure + AU Runways / Pan-AU State Fire Trails & Tracks / HV Power / Pipelines
    if au_aviation_path and "au_runways" in au_aviation_layers:
        _push(_load_vector_layer(project, au_aviation_path, "au_runways", "AU Runways", role="au_runways", workspace=workspace))
    for label_prefix, st_path, st_layers in au_state_trail_entries:
        if "au_state_trails" in st_layers:
            _push(
                _load_vector_layer(
                    project,
                    st_path,
                    "au_state_trails",
                    f"{label_prefix} Fire Trails & Tracks",
                    role="au_state_trails",
                    workspace=workspace,
                )
            )
    if "lines" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "lines", "Roads & Transport", role="transport", workspace=workspace))
    if au_infra_path and "au_power_lines" in au_infra_layers and carto.show_power:
        _push(_load_vector_layer(project, au_infra_path, "au_power_lines", "AU Transmission Lines", role="au_power_lines", workspace=workspace))
    if au_infra_path and "au_pipelines" in au_infra_layers and carto.show_pipelines:
        _push(_load_vector_layer(project, au_infra_path, "au_pipelines", "AU Pipelines", role="au_pipelines", workspace=workspace))

    # 6. OSM Buildings & Power Substations
    if "multipolygons" in osm_layers and (carto.show_buildings or carto.show_power):
        _push(_load_vector_layer(project, workspace.osm_gpkg, "multipolygons", "Buildings", role="buildings", workspace=workspace))

    # 7. Waterways (OSM + optional USGS NHD + optional GA Hydrology + optional NOAA ENC Depth Contours)
    if "lines" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "lines", "Waterways", role="waterways", workspace=workspace))
    if au_hydro_path and "au_hydro_lines" in au_hydro_layers:
        _push(_load_vector_layer(project, au_hydro_path, "au_hydro_lines", "AU Watercourses", role="au_hydro_lines", workspace=workspace))
    if nhd_path and "nhd_flowlines" in nhd_layers:
        _push(_load_vector_layer(project, nhd_path, "nhd_flowlines", "USGS NHD Flowlines", role="nhd_flowlines", workspace=workspace))
    if enc_path and "enc_depth_contours" in enc_layers:
        _push(
            _load_vector_layer(
                project, enc_path, "enc_depth_contours", "NOAA ENC Depth Contours", role="enc_depth_contours", workspace=workspace
            )
        )

    # 8. Elevation Contours (Index + Minor with curved haloed labels)
    if "contours" in terrain_layers:
        _push(_load_vector_layer(project, workspace.terrain_gpkg, "contours", "Contours", role="contours", workspace=workspace))

    # 9. Water Polygons (OSM + optional GA Surface Hydrology polygons; above hillshade)
    if "multipolygons" in osm_layers:
        _push(
            _load_vector_layer(
                project, workspace.osm_gpkg, "multipolygons", "Waterbodies", role="water_polygons", workspace=workspace
            )
        )
    if au_hydro_path and "au_hydro_polys" in au_hydro_layers:
        _push(_load_vector_layer(project, au_hydro_path, "au_hydro_polys", "AU Waterbodies", role="au_hydro_polys", workspace=workspace))

    # 10. Regional Area Overlays (FEMA Flood Zones + CAPAD AU Parks/IPAs + US/CA Public Lands; strictly below contours & roads!)
    if fema_path and "fema_flood" in fema_layers:
        _push(_load_vector_layer(project, fema_path, "fema_flood", "FEMA Flood Zones", role="fema_flood", workspace=workspace))
    if au_parks_path and "au_protected_areas" in au_parks_layers:
        _push(_load_vector_layer(project, au_parks_path, "au_protected_areas", "AU Protected Areas", role="au_protected_areas", workspace=workspace))
    for pl_label, pl_path in public_lands_entries:
        _push(_load_vector_layer(project, pl_path, "public_lands", pl_label, role="public_lands", workspace=workspace))

    # 10.5. Slope-Angle Avalanche Shading (bottom raster stack right above viewshed & hillshade; vectors stay pure!)
    if carto.show_slope_shading and workspace.slope_tif is not None and workspace.slope_tif.is_file():
        slope_layer = QgsRasterLayer(str(workspace.slope_tif), "Slope Shading")
        if slope_layer.isValid():
            apply_layer_style(
                slope_layer,
                "slope_shading",
                spec.color_mode,
                units=spec.units,
                scale=spec.scale,
                carto=carto,
            )
            project.addMapLayer(slope_layer, False)
            ordered_top_to_bottom.append(slope_layer)

    # 10.75. RF / Line-of-Sight Viewshed Coverage (bottom raster stack right above hillshade, below slope!)
    if workspace.viewshed_tif is not None and workspace.viewshed_tif.is_file():
        vs_layer = QgsRasterLayer(str(workspace.viewshed_tif), "LOS Viewshed")
        if vs_layer.isValid():
            apply_layer_style(
                vs_layer,
                "viewshed",
                spec.color_mode,
                units=spec.units,
                scale=spec.scale,
                carto=carto,
            )
            project.addMapLayer(vs_layer, False)
            ordered_top_to_bottom.append(vs_layer)

    # 11. Multi-directional Hillshade (Multiply blend over landcover + background; omitted if OFF)
    if carto.hillshade_strength != HillshadeStrength.OFF and workspace.hillshade_tif.is_file():
        hs_layer = QgsRasterLayer(str(workspace.hillshade_tif), "Hillshade")
        if hs_layer.isValid():
            apply_layer_style(
                hs_layer,
                "hillshade",
                spec.color_mode,
                units=spec.units,
                scale=spec.scale,
                carto=carto,
            )
            project.addMapLayer(hs_layer, False)
            ordered_top_to_bottom.append(hs_layer)

    # 12. OSM Landcover Polygons (bottom layer — receives Multiply hillshade & slope shading!)
    if "multipolygons" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "multipolygons", "Landcover", role="landcover", workspace=workspace))

    return ordered_top_to_bottom


def export_qml_styles(styles_root: Path | None = None) -> list[Path]:
    """Generate native QGIS 3.40 ``.qml`` files for both Color and B&W themes.

    Must be executed with an initialized ``QgsApplication``.
    """
    import tempfile

    from osgeo import gdal
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    root = styles_root if styles_root is not None else get_styles_root()
    written: list[Path] = []

    vector_specs: dict[str, tuple[str, str]] = {
        "landcover": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=name:string&field=natural:string&field=landuse:string"
            "&field=leaf_type:string&field=wood:string&field=leaf_cycle:string"
            "&field=water:string&field=waterway:string&field=building:string"
            "&field=amenity:string&field=religion:string&field=emergency:string"
            "&field=leisure:string&field=aeroway:string&field=power:string"
            "&field=man_made:string&field=boundary:string",
            "landcover",
        ),
        "fema_flood": (
            "MultiPolygon?crs=EPSG:32613&field=fld_zone:string&field=zone_subty:string&field=sfha_tf:string",
            "fema_flood",
        ),
        "au_protected_areas": (
            "MultiPolygon?crs=EPSG:32752"
            "&field=name:string&field=park_type:string&field=type_abbr:string&field=iucn:string&field=state:string",
            "au_protected_areas",
        ),
        "public_lands": (
            "MultiPolygon?crs=EPSG:32613"
            "&field=name:string&field=manager:string&field=designation:string&field=state:string",
            "public_lands",
        ),
        "water_polygons": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=name:string&field=natural:string&field=landuse:string"
            "&field=water:string&field=waterway:string",
            "water_polygons",
        ),
        "au_hydro_polys": (
            "MultiPolygon?crs=EPSG:32752&field=name:string&field=featuretype:string&field=perenniality:string",
            "au_hydro_polys",
        ),
        "contours": (
            "LineString?crs=EPSG:32756&field=elev:double&field=elev_label:string&field=is_index:integer",
            "contours",
        ),
        "enc_depth_contours": (
            "LineString?crs=EPSG:32613&field=valdco:double",
            "enc_depth_contours",
        ),
        "nhd_flowlines": (
            "LineString?crs=EPSG:32613&field=gnis_name:string&field=fcode:integer&field=flow_type:string",
            "nhd_flowlines",
        ),
        "au_hydro_lines": (
            "LineString?crs=EPSG:32752"
            "&field=name:string&field=featuretype:string&field=perenniality:string&field=hierarchy:string",
            "au_hydro_lines",
        ),
        "waterways": (
            "LineString?crs=EPSG:32756&field=name:string&field=waterway:string&field=intermittent:string",
            "waterways",
        ),
        "buildings": (
            "MultiPolygon?crs=EPSG:32756&field=name:string&field=building:string&field=power:string",
            "buildings",
        ),
        "building_icons": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=name:string&field=building:string&field=amenity:string"
            "&field=emergency:string&field=aeroway:string&field=religion:string",
            "building_icons",
        ),
        "transport": (
            "LineString?crs=EPSG:32756"
            "&field=name:string&field=highway:string&field=ref:string&field=surface:string"
            "&field=tracktype:string&field=railway:string&field=aeroway:string"
            "&field=power:string&field=voltage:string&field=man_made:string"
            "&field=substance:string&field=location:string&field=bridge:string"
            "&field=tunnel:string&field=layer:string&field=oneway:string"
            "&field=natural:string&field=barrier:string&field=ford:string",
            "transport",
        ),
        "au_power_lines": (
            "LineString?crs=EPSG:32756&field=name:string&field=capacity_kv:double&field=status:string&field=state:string",
            "au_power_lines",
        ),
        "au_state_trails": (
            "LineString?crs=EPSG:32756&field=name:string&field=trail_class:string&field=surface:string&field=state:string",
            "au_state_trails",
        ),
        "au_pipelines": (
            "LineString?crs=EPSG:32756&field=name:string&field=pipeline_type:string&field=status:string&field=operator:string",
            "au_pipelines",
        ),
        "au_runways": (
            "LineString?crs=EPSG:32752&field=featuretype:string&field=type:string",
            "au_runways",
        ),
        "points": (
            "Point?crs=EPSG:32756"
            "&field=name:string&field=highway:string&field=ref:string&field=exit_to:string"
            "&field=junction:string&field=railway:string&field=aeroway:string"
            "&field=natural:string&field=ele:string&field=amenity:string&field=building:string"
            "&field=religion:string&field=emergency:string&field=shop:string"
            "&field=historic:string&field=place:string&field=power:string"
            "&field=man_made:string&field=tower:type:string&field=communication:string"
            "&field=tourism:string&field=waterway:string&field=barrier:string&field=ford:string",
            "points",
        ),
        "au_state_pois": (
            "Point?crs=EPSG:32756&field=name:string&field=poi_type:string&field=elev_m:double&field=state:string",
            "au_state_pois",
        ),
        "au_substations": (
            "Point?crs=EPSG:32756&field=name:string&field=voltage_kv:double&field=status:string&field=state:string",
            "au_substations",
        ),
        "au_hydro_points": (
            "Point?crs=EPSG:32752&field=name:string&field=featuretype:string&field=type:string&field=perenniality:string",
            "au_hydro_points",
        ),
        "au_homesteads": (
            "Point?crs=EPSG:32752&field=name:string&field=featuretype:string",
            "au_homesteads",
        ),
        "au_airfields": (
            "Point?crs=EPSG:32752&field=name:string&field=featuretype:string&field=type:string",
            "au_airfields",
        ),
        "us_towers": (
            "Point?crs=EPSG:32613"
            "&field=source:string&field=name:string&field=obstacle_type:string"
            "&field=agl_ft:double&field=amsl_ft:double&field=lighting:string",
            "us_towers",
        ),
        "enc_navaids": (
            "Point?crs=EPSG:32613&field=objnam:string&field=navaid_type:string&field=colour:string",
            "enc_navaids",
        ),
        "mgrs_grid_1km": (
            "LineString?crs=EPSG:32756&field=label:string&field=axis:string&field=coord_m:integer&field=is_100km:integer",
            "mgrs_grid_1km",
        ),
        "mgrs_grid_100km": (
            "LineString?crs=EPSG:32756&field=label:string&field=axis:string&field=coord_m:integer",
            "mgrs_grid_100km",
        ),
        "mgrs_zone_boundary": ("LineString?crs=EPSG:32756&field=label:string", "mgrs_zone_boundary"),
        "mgrs_square_labels": (
            "Point?crs=EPSG:32756&field=gzd:string&field=square_id:string&field=full_id:string",
            "mgrs_square_labels",
        ),
        "overlay_polygons": ("MultiPolygon?crs=EPSG:32756&field=name:string", "overlay_polygons"),
        "overlay_lines": ("LineString?crs=EPSG:32756&field=name:string", "overlay_lines"),
        "overlay_points": ("Point?crs=EPSG:32756&field=name:string", "overlay_points"),
        "tactical_sectors": (
            "MultiPolygon?crs=EPSG:32756"
            "&field=label:string&field=kind:string&field=area_km2:double&field=color:string&field=notes:string",
            "tactical_sectors",
        ),
        "tactical_rings": (
            "LineString?crs=EPSG:32756"
            "&field=label:string&field=base_label:string&field=radius_km:double"
            "&field=radius_m:double&field=color:string&field=notes:string",
            "tactical_rings",
        ),
        "tactical_lines": (
            "LineString?crs=EPSG:32756"
            "&field=label:string&field=kind:string&field=distance_km:double"
            "&field=bearing_true_deg:double&field=bearing_grid_deg:double"
            "&field=bearing_mag_deg:double&field=bearing_mils:double&field=color:string&field=notes:string",
            "tactical_lines",
        ),
        "waypoint_route": ("LineString?crs=EPSG:32756&field=total_km:double", "waypoint_route"),
        "waypoint_points": (
            "Point?crs=EPSG:32756&field=seq:integer&field=code:string&field=label:string"
            "&field=category:string&field=mgrs_10m:string&field=elev_m:double",
            "waypoint_points",
        ),
        "viewshed_rings": (
            "LineString?crs=EPSG:32756&field=label:string&field=max_distance_m:double",
            "viewshed_rings",
        ),
        "viewshed_observers": (
            "Point?crs=EPSG:32756&field=label:string&field=observer_height_m:double"
            "&field=target_height_m:double&field=max_distance_m:double&field=elev_m:double",
            "viewshed_observers",
        ),
    }

    with tempfile.TemporaryDirectory(prefix="gridforge_qml_") as tmpdir:
        gdal.UseExceptions()
        dummy_tif = Path(tmpdir) / "dummy_hillshade.tif"
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(str(dummy_tif), 4, 4, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        ds.GetRasterBand(1).Fill(190)
        ds.FlushCache()
        ds = None

        dummy_slope_tif = Path(tmpdir) / "dummy_slope_rgba.tif"
        sds = drv.Create(str(dummy_slope_tif), 4, 4, 4, gdal.GDT_Byte)
        sds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        for b_idx, val in ((1, 229), (2, 62), (3, 62), (4, 185)):
            sds.GetRasterBand(b_idx).Fill(val)
        sds.FlushCache()
        sds = None

        dummy_vs_tif = Path(tmpdir) / "dummy_viewshed_rgba.tif"
        vds = drv.Create(str(dummy_vs_tif), 4, 4, 4, gdal.GDT_Byte)
        vds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        for b_idx, val in ((1, 0), (2, 212), (3, 146), (4, 130)):
            vds.GetRasterBand(b_idx).Fill(val)
        vds.FlushCache()
        vds = None

        def _tag_native_qml(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            marker = f"<!-- {_NATIVE_QML_MARKER} -->\n"
            if _NATIVE_QML_MARKER not in text:
                if text.startswith("<!DOCTYPE"):
                    first_nl = text.find("\n")
                    text = text[: first_nl + 1] + marker + text[first_nl + 1 :]
                else:
                    text = marker + text
                path.write_text(text, encoding="utf-8")

        for mode in (ColorMode.COLOR, ColorMode.BW, ColorMode.NIGHT_RED, ColorMode.ALPINE):
            mode_dir = root / mode.value
            mode_dir.mkdir(parents=True, exist_ok=True)

            rlayer = QgsRasterLayer(str(dummy_tif), "hillshade")
            apply_programmatic_style(rlayer, "hillshade", mode)
            hs_qml = mode_dir / "hillshade.qml"
            msg, ok = rlayer.saveNamedStyle(str(hs_qml))
            if not ok:
                raise RuntimeError(f"Failed to export {hs_qml}: {msg}")
            _tag_native_qml(hs_qml)
            written.append(hs_qml)

            vlayer_vs = QgsRasterLayer(str(dummy_vs_tif), "viewshed")
            apply_programmatic_style(vlayer_vs, "viewshed", mode)
            vs_qml = mode_dir / "viewshed.qml"
            msg, ok = vlayer_vs.saveNamedStyle(str(vs_qml))
            if not ok:
                raise RuntimeError(f"Failed to export {vs_qml}: {msg}")
            _tag_native_qml(vs_qml)
            written.append(vs_qml)

            slayer = QgsRasterLayer(str(dummy_slope_tif), "slope_shading")
            apply_programmatic_style(slayer, "slope_shading", mode)
            slope_qml = mode_dir / "slope_shading.qml"
            msg, ok = slayer.saveNamedStyle(str(slope_qml))
            if not ok:
                raise RuntimeError(f"Failed to export {slope_qml}: {msg}")
            _tag_native_qml(slope_qml)
            written.append(slope_qml)

            for role, (uri, layer_name) in vector_specs.items():
                vlayer = QgsVectorLayer(uri, layer_name, "memory")
                apply_programmatic_style(vlayer, role, mode, units=Units.METRIC, scale=25000)
                qml_path = mode_dir / f"{role}.qml"
                msg, ok = vlayer.saveNamedStyle(str(qml_path))
                if not ok:
                    raise RuntimeError(f"Failed to export {qml_path}: {msg}")
                _tag_native_qml(qml_path)
                written.append(qml_path)

    return written


if __name__ == "__main__":
    import gc
    import os
    import sys

    from qgis.core import QgsApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
    Path(os.environ["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        target = Path(sys.argv[1]) if len(sys.argv) > 1 else get_styles_root()
        paths = export_qml_styles(target)
        print(f"Exported {len(paths)} QGIS 3.40 QML files to {target}")
    finally:
        gc.collect()
        qgs.exitQgis()
