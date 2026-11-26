"""Unit tests for Western Australia, South Australia, and Northern Territory state trail overlays."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from osgeo import ogr, osr

from gridforge.config import PaperSize, SheetSpec
from gridforge.geo.crs import compute_sheet_layout
from gridforge.packs.builder import build_offline_pack
from gridforge.sources.overlays_au_states_west import (
    DEFAULT_SA_TRAILS_ZIP_INNER,
    NTTrailsSource,
    SATrailsSource,
    WATrailsSource,
    is_in_nt_coverage,
    is_in_sa_coverage,
    is_in_wa_coverage,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _read_gpkg_layers(gpkg_path: Path) -> dict[str, list[dict[str, Any]]]:
    ogr.UseExceptions()
    ds = ogr.Open(str(gpkg_path))
    assert ds is not None, f"Could not open GeoPackage {gpkg_path}"
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            rows: list[dict[str, Any]] = []
            for feat in lyr:
                rows.append(dict(feat.items()))
            out[lyr.GetName()] = rows
    finally:
        ds = None
    return out


def _make_local_state_trails_gpkg(path: Path, lon: float, lat: float, state: str) -> Path:
    ogr.UseExceptions()
    osr.UseExceptions()
    driver = ogr.GetDriverByName("GPKG")
    if path.exists():
        path.unlink()
    ds = driver.CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    lt = ds.CreateLayer("au_state_trails", srs=srs, geom_type=ogr.wkbMultiLineString)
    for fname in ("name", "trail_class", "surface", "state"):
        lt.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    line = ogr.Geometry(ogr.wkbLineString)
    line.AddPoint_2D(lon - 0.01, lat - 0.01)
    line.AddPoint_2D(lon + 0.01, lat + 0.01)
    ml = ogr.Geometry(ogr.wkbMultiLineString)
    ml.AddGeometry(line)
    ft = ogr.Feature(lt.GetLayerDefn())
    ft.SetGeometry(ml)
    ft.SetField("name", f"{state} Test Fire Trail")
    ft.SetField("trail_class", "fire_trail")
    ft.SetField("surface", "unsealed")
    ft.SetField("state", state)
    lt.CreateFeature(ft)

    lp = ds.CreateLayer("au_state_pois", srs=srs, geom_type=ogr.wkbPoint)
    lp.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lp.CreateField(ogr.FieldDefn("poi_type", ogr.OFTString))
    lp.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
    lp.CreateField(ogr.FieldDefn("state", ogr.OFTString))
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint_2D(lon, lat)
    fp = ogr.Feature(lp.GetLayerDefn())
    fp.SetGeometry(pt)
    fp.SetField("name", f"{state} Summit Trig")
    fp.SetField("poi_type", "trig")
    fp.SetField("elev_m", 1095.8)
    fp.SetField("state", state)
    lp.CreateFeature(fp)

    ds.FlushCache()
    ds = None
    return path


class TestWATrailsSource:
    def test_wa_normalization_and_outback_geodetic_fallback(self, tmp_path: Path) -> None:
        # Stirling Range / Bluff Knoll (-34.375, 118.255 -> EPSG:32750)
        layout = compute_sheet_layout(
            SheetSpec(center_lat=-34.375, center_lon=118.255, scale=25000, paper=PaperSize.A4)
        )
        assert is_in_wa_coverage(layout.wgs84_fetch_bbox)
        assert not is_in_sa_coverage(layout.wgs84_fetch_bbox)
        assert not is_in_nt_coverage(layout.wgs84_fetch_bbox)

        def _handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if "Tracks_and_Trails_hosted" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[118.250, -34.378], [118.260, -34.372]],
                                },
                                "properties": {"TYPE": "Bibbulmun Track"},
                            }
                        ],
                    },
                )
            if "/Transport/MapServer/20" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[118.252, -34.380], [118.258, -34.370]],
                                },
                                "properties": {
                                    "road_name": "Bluff Fire Access Track",
                                    "fcsubtype": "Track",
                                    "mapclassification": "Track",
                                    "roadsurface": "Unsealed",
                                },
                            },
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[118.251, -34.376], [118.259, -34.374]],
                                },
                                "properties": {
                                    "road_name": "",
                                    "fcsubtype": "Track",
                                    "mapclassification": "Track",
                                    "roadsurface": "Unsealed",
                                },
                            },
                        ],
                    },
                )
            if "RATIS_Sites" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [118.255, -34.375]},
                                "properties": {
                                    "RTS_NAME": "Bluff Knoll Camp Site",
                                    "RTS_PRIMARY_FUNCTION": "Day Use and Camping",
                                    "RTS_STATUS": "OPEN",
                                },
                            },
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [118.256, -34.374]},
                                "properties": {
                                    "RTS_NAME": "Eastern Lookout",
                                    "RTS_PRIMARY_FUNCTION": "Day Use Only",
                                    "RTS_STATUS": "OPEN",
                                },
                            },
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [118.257, -34.376]},
                                "properties": {
                                    "RTS_NAME": "Closed Depot",
                                    "RTS_PRIMARY_FUNCTION": "Management Site Only",
                                    "RTS_STATUS": "OPEN",
                                },
                            },
                        ],
                    },
                )
            if "RATIS_Facilities" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [118.254, -34.373]},
                                "properties": {
                                    "RTT_FAC_TYPE": "STRUCTURE",
                                    "RTT_SUB_TYPE_DESC": "Camp Shelter",
                                    "RTT_ASSET_DESC": "Hewetts Hill Hut",
                                    "RTT_ASSET_STATUS": "Active",
                                },
                            }
                        ],
                    },
                )
            if "/Environment/MapServer/56" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [118.253, -34.376]},
                                "properties": {
                                    "ftw_name": "Bickley Tower",
                                    "ftw_type": "CALM_TOWER",
                                    "ftw_status": "Operational",
                                },
                            }
                        ],
                    },
                )
            if "/Property_and_Planning/MapServer/1" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [118.256, -34.377]},
                                "properties": {
                                    "geodetic_point_name": "MOUNT BARKER 117",
                                    "render_value": "S1  ",
                                    "filter_value": 1,
                                    "reduced_level": 1095.8,
                                    "vert_datum": "AHD71",
                                    "latest_status_description": "IN PLACE",
                                },
                            },
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [118.258, -34.377]},
                                "properties": {
                                    "geodetic_point_name": "GONE MARK",
                                    "render_value": "S1  ",
                                    "filter_value": 1,
                                    "reduced_level": 900.0,
                                    "vert_datum": "AHD71",
                                    "latest_status_description": "DESTROYED",
                                },
                            },
                        ],
                    },
                )
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

        client = httpx.Client(transport=httpx.MockTransport(_handler))
        try:
            src = WATrailsSource(cache_dir=tmp_path / "cache", client=client)
            assert "CC BY 4.0" in src.attribution
            assert "CC BY-NC" not in src.attribution
            out = src.fetch_overlay(layout, tmp_path / "work")
        finally:
            client.close()

        assert out is not None and out.is_file()
        layers = _read_gpkg_layers(out)
        assert len(layers["au_state_trails"]) == 3
        classes = {r["trail_class"] for r in layers["au_state_trails"]}
        assert classes == {"walking_track", "fire_trail", "4wd_track"}
        assert all(r["state"] == "WA" for r in layers["au_state_trails"])

        pois = {(r["poi_type"], r["name"]): r["elev_m"] for r in layers["au_state_pois"]}
        assert ("campground", "Bluff Knoll Camp Site") in pois
        assert ("lookout", "Eastern Lookout") in pois
        assert ("lookout", "Bickley Tower") in pois
        assert ("hut", "Hewetts Hill Hut") in pois
        assert pois[("trig", "MOUNT BARKER 117")] == pytest.approx(1095.8)
        assert all(name != "Closed Depot" and name != "GONE MARK" for _, name in pois)

    def test_wa_out_of_state_short_circuit_and_local_gpkg(self, tmp_path: Path) -> None:
        # Blue Mountains NSW is strictly outside WA
        nsw_layout = compute_sheet_layout(
            SheetSpec(center_lat=-33.732, center_lon=150.312, scale=25000, paper=PaperSize.A4)
        )
        src = WATrailsSource(cache_dir=tmp_path / "cache")
        assert src.fetch_overlay(nsw_layout, tmp_path / "out_none") is None

        # Air-gapped local_gpkg works even outside online state bounds
        local_gpkg = _make_local_state_trails_gpkg(
            tmp_path / "local_wa.gpkg", 150.312, -33.732, "WA"
        )
        src_local = WATrailsSource(local_gpkg=local_gpkg, cache_dir=tmp_path / "cache")
        clipped = src_local.fetch_overlay(nsw_layout, tmp_path / "out_local")
        assert clipped is not None and clipped.is_file()
        layers = _read_gpkg_layers(clipped)
        assert len(layers["au_state_trails"]) == 1
        assert len(layers["au_state_pois"]) == 1


class TestSATrailsSource:
    def test_sa_vsizip_spatial_filter_fire_tracks_parks_and_survey_marks(
        self, tmp_path: Path
    ) -> None:
        # Flinders Ranges / Wilpena Pound (-31.530, 138.600 -> EPSG:32754)
        layout = compute_sheet_layout(
            SheetSpec(center_lat=-31.530, center_lon=138.600, scale=25000, paper=PaperSize.A4)
        )
        assert is_in_sa_coverage(layout.wgs84_fetch_bbox)

        # Create a real zipped GeoJSON with 1 trail INSIDE Flinders bbox and 1 trail FAR AWAY!
        synthetic_gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[138.595, -31.535], [138.605, -31.525]],
                    },
                    "properties": {
                        "TRAILNAME": "Heysen Trail",
                        "TRAILTYPE": "WALKING",
                        "TRAILSTATU": "OPEN",
                    },
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[135.100, -34.800], [135.200, -34.850]],
                    },
                    "properties": {
                        "TRAILNAME": "Far Away Eyre Trail",
                        "TRAILTYPE": "WALKING",
                        "TRAILSTATU": "OPEN",
                    },
                },
            ],
        }
        zip_path = tmp_path / "synthetic_sa_trails.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(DEFAULT_SA_TRAILS_ZIP_INNER, json.dumps(synthetic_gj))

        parks_payload = [
            {
                "name": "Ikara-Flinders Ranges National Park",
                "lat": -31.5292,
                "lng": 138.6041,
                "campingAndAccommodation": {"Caravans sites": True, "Tent sites": True},
            },
            {
                "name": "Mount Lofty Summit",
                "lat": -34.9756,
                "lng": 138.7096,
                "campingAndAccommodation": {"Caravans sites": False},
            },
        ]

        seen_where_clauses: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if "parks-data.json" in u:
                return httpx.Response(200, json=parks_payload)
            if "Fire_Management_Fire_Access_Tracks" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[138.598, -31.532], [138.604, -31.528]],
                                },
                                "properties": {
                                    "TRACK_NAME": "Wilpena Fire Access",
                                    "RESERVE_NAME": "Ikara-Flinders Ranges NP",
                                },
                            }
                        ],
                    },
                )
            if "Topographic_Base_Map/MapServer/434" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[138.590, -31.531], [138.608, -31.529]],
                                },
                                "properties": {
                                    "name": "Bunyeroo 4WD Track",
                                    "class": "Track",
                                    "formation": "Unsealed",
                                },
                            }
                        ],
                    },
                )
            if "MAD_WMS_PRD/MapServer/0" in u:
                seen_where_clauses.append(request.url.params.get("where", ""))
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [138.601, -31.530]},
                                "properties": {
                                    "MARK_NO": "6635/101",
                                    "MARKTYPE": "PSMP",
                                    "H_ORDER": 2,
                                    "V_ORDER": 2,
                                    "V_DATUM": "A",
                                    "HEIGHT": 1171.4,
                                    "GONE": None,
                                },
                            },
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [138.602, -31.531]},
                                "properties": {
                                    "MARK_NO": "6635/999",
                                    "MARKTYPE": "PEG",
                                    "H_ORDER": 1,
                                    "V_ORDER": 1,
                                    "V_DATUM": "A",
                                    "HEIGHT": 500.0,
                                    "GONE": "Y",
                                },
                            },
                        ],
                    },
                )
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

        client = httpx.Client(transport=httpx.MockTransport(_handler))
        try:
            src = SATrailsSource(
                trails_zip_path=zip_path,
                cache_dir=tmp_path / "cache",
                client=client,
            )
            out = src.fetch_overlay(layout, tmp_path / "work")
        finally:
            client.close()

        assert out is not None and out.is_file()
        # Confirm MAD_WMS_PRD strictly used where="1=1" (zero slow server-side LIKE!)
        assert seen_where_clauses == ["1=1"]

        layers = _read_gpkg_layers(out)
        trail_names = {r["name"] for r in layers["au_state_trails"]}
        assert "Heysen Trail" in trail_names
        assert "Far Away Eyre Trail" not in trail_names  # Filtered out by GDAL /vsizip/ spatial filter!
        assert "Wilpena Fire Access" in trail_names
        assert "Bunyeroo 4WD Track" in trail_names
        assert {r["trail_class"] for r in layers["au_state_trails"]} == {
            "walking_track",
            "fire_trail",
            "4wd_track",
        }

        pois = {(r["poi_type"], r["name"]): r["elev_m"] for r in layers["au_state_pois"]}
        assert ("campground", "Ikara-Flinders Ranges National Park") in pois
        assert pois[("trig", "SM 6635/101")] == pytest.approx(1171.4)
        assert ("trig", "SM 6635/999") not in pois  # GONE='Y' filtered out

    def test_sa_out_of_state_short_circuit(self, tmp_path: Path) -> None:
        perth_layout = compute_sheet_layout(
            SheetSpec(center_lat=-31.98, center_lon=116.16, scale=25000, paper=PaperSize.A4)
        )
        assert SATrailsSource(cache_dir=tmp_path / "cache").fetch_overlay(perth_layout, tmp_path / "w") is None


class TestNTTrailsSource:
    def test_nt_wfs_and_roads_normalization(self, tmp_path: Path) -> None:
        # West MacDonnell Ranges / Larapinta (-23.730, 133.240 -> EPSG:32753)
        layout = compute_sheet_layout(
            SheetSpec(center_lat=-23.730, center_lon=133.240, scale=25000, paper=PaperSize.A4)
        )
        assert is_in_nt_coverage(layout.wgs84_fetch_bbox)

        def _handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if "ROAD_CENTRELINES" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[133.235, -23.735], [133.245, -23.725]],
                                },
                                "properties": {
                                    "ROAD_LABEL": "*4WD HUGH GORGE TRACK",
                                    "SURFACE_TYPE": "4WD",
                                    "CATEGORY": "PASTORAL - LOCAL",
                                },
                            },
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[133.236, -23.734], [133.244, -23.726]],
                                },
                                "properties": {
                                    "ROAD_LABEL": "NAMATJIRA DRIVE",
                                    "SURFACE_TYPE": "SEALED",
                                    "CATEGORY": "PASTORAL - SECONDARY",
                                },
                            },
                        ],
                    },
                )
            if "Topographic_Base_Map/MapServer/434" in u:
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {
                                    "type": "LineString",
                                    "coordinates": [[133.237, -23.733], [133.243, -23.727]],
                                },
                                "properties": {
                                    "name": " ",
                                    "class": "Track",
                                    "formation": "Unsealed",
                                },
                            }
                        ],
                    },
                )
            if request.url.params.get("typeNames") == "ntlis:PLACENAMES":
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [133.240, -23.730]},
                                "properties": {"NAME": "Brinkley Bluff", "TYPE_DESIGNATION": "Cliff"},
                            },
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [133.242, -23.728]},
                                "properties": {
                                    "NAME": "Watarrka Ranger Station",
                                    "TYPE_DESIGNATION": "Ranger Station",
                                },
                            },
                        ],
                    },
                )
            if request.url.params.get("typeNames") == "survey:SURVEY_MARKS":
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [133.241, -23.731]},
                                "properties": {
                                    "PRIMARYNAME": "NTS044",
                                    "MARKTYPE": "CRM",
                                    "ADJ_RL": None,
                                    "QUASI_AHD": 1208.9,
                                    "VERTICALDATUM": "AHD",
                                },
                            },
                            {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [133.243, -23.732]},
                                "properties": {
                                    "PRIMARYNAME": "NTL503",
                                    "MARKTYPE": "BM",
                                    "ADJ_RL": None,
                                    "QUASI_AHD": 0,
                                    "VERTICALDATUM": "AHD",
                                },
                            },
                        ],
                    },
                )
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

        client = httpx.Client(transport=httpx.MockTransport(_handler))
        try:
            src = NTTrailsSource(cache_dir=tmp_path / "cache", client=client)
            out = src.fetch_overlay(layout, tmp_path / "work")
        finally:
            client.close()

        assert out is not None and out.is_file()
        layers = _read_gpkg_layers(out)
        # SEALED Namatjira Drive skipped; 4WD track + GA Track retained, leading '*' stripped!
        assert len(layers["au_state_trails"]) == 2
        assert any(r["name"] == "4WD HUGH GORGE TRACK" for r in layers["au_state_trails"])
        assert all(r["name"] != "NAMATJIRA DRIVE" for r in layers["au_state_trails"])

        pois = {(r["poi_type"], r["name"]): r["elev_m"] for r in layers["au_state_pois"]}
        assert ("lookout", "Brinkley Bluff") in pois
        assert ("hut", "Watarrka Ranger Station") in pois
        assert pois[("trig", "NTS044")] == pytest.approx(1208.9)
        assert ("trig", "NTL503") not in pois

    def test_nt_out_of_state_short_circuit_and_pack_builder_includes_state_trails(
        self, tmp_path: Path
    ) -> None:
        tas_layout = compute_sheet_layout(
            SheetSpec(center_lat=-41.684, center_lon=145.951, scale=25000, paper=PaperSize.A4)
        )
        assert NTTrailsSource(cache_dir=tmp_path / "cache").fetch_overlay(tas_layout, tmp_path / "w") is None

        # Verify build_offline_pack bundles au-state-trails cleanly via local_overlay_overrides
        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        osm_fixture = FIXTURES_DIR / "synthetic_osm.osm"
        mock_trails = _make_local_state_trails_gpkg(
            tmp_path / "mock_state_trails.gpkg", 150.312, -33.732, "NSW"
        )
        manifest = build_offline_pack(
            "150.22,-33.80,150.40,-33.66",
            tmp_path / "pack_out",
            include_osm=True,
            include_dem=True,
            include_overlays=True,
            source_pbf=osm_fixture,
            source_dem=dem_fixture,
            local_overlay_overrides={"au-state-trails": mock_trails},
            cache_dir=tmp_path / "cache",
        )
        assert "au-state-trails" in manifest.overlays
        assert (tmp_path / "pack_out" / manifest.overlays["au-state-trails"]).is_file()
