"""Unit tests for military cartography, icons, CartoOptions toggles, AU overlays, and pack runner wiring.

Note: PyQGIS tests execute inside spawned subprocesses so ``QgsApplication.exitQgis()``
never calls ``GDALDestroyDriverManager()`` inside the shared ``pytest`` runner process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

from gridforge.config import (
    AUOverlayKind,
    BBox,
    ColorMode,
    DemSourceKind,
    PackManifest,
    RenderResult,
    SheetSpec,
)
from gridforge.pipeline.runner import _resolve_elevation_source, run_render_job
from gridforge.render.styles import (
    STYLE_ROLES,
    get_icon_path,
    get_icons_root,
    scale_font_delta,
    scale_width_factor,
)
from gridforge.sources.dem_au_elvis import AUElvisDEMSource


def _run_pyqgis_snippet(body: str) -> None:
    """Execute a PyQGIS assertion snippet in a fresh spawned subprocess."""
    script = (
        "import gc, os\n"
        "from pathlib import Path\n"
        "from qgis.core import QgsApplication\n"
        "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
        "rd = Path(os.environ.setdefault('XDG_RUNTIME_DIR', '/tmp/runtime-root'))\n"
        "rd.mkdir(parents=True, exist_ok=True)\n"
        "QgsApplication.setPrefixPath(os.environ.get('QGIS_PREFIX_PATH', '/usr'), True)\n"
        "qgs = QgsApplication([], False)\n"
        "qgs.initQgis()\n"
        "def _run():\n"
        + textwrap.indent(textwrap.dedent(body).strip("\n"), "    ")
        + "\n"
        "try:\n"
        "    _run()\n"
        "finally:\n"
        "    gc.collect()\n"
        "    qgs.exitQgis()\n"
    )
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"PyQGIS subprocess test failed (exit={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_all_svg_icons_exist_and_parse_xml() -> None:
    expected = (
        "hospital.svg",
        "fire_station.svg",
        "police.svg",
        "school.svg",
        "worship.svg",
        "fuel.svg",
        "shelter.svg",
        "helipad.svg",
        "comm_tower.svg",
        "obstacle_tower.svg",
        "saddle.svg",
        "river_arrow.svg",
        "conifer.svg",
        "deciduous.svg",
        "navaid.svg",
        "water_bore.svg",
        "homestead.svg",
        "airfield.svg",
        "hut.svg",
        "campground.svg",
        "trig.svg",
    )
    root = get_icons_root()
    assert root.is_dir()
    assert len(expected) == 21
    for name in expected:
        p = get_icon_path(name, icons_root=root)
        assert p.is_file(), f"Missing icon: {p}"
        elem = ET.fromstring(p.read_text(encoding="utf-8"))
        assert elem.tag.endswith("svg")


def test_multiscale_factors() -> None:
    assert scale_width_factor(15000) > scale_width_factor(25000) > scale_width_factor(50000)
    assert scale_font_delta(15000) > scale_font_delta(25000) > scale_font_delta(50000)


def test_all_qml_styles_roundtrip(tmp_path: Path) -> None:
    assert len(STYLE_ROLES) == 36
    _run_pyqgis_snippet(
        f"""
        from osgeo import gdal
        from qgis.core import QgsRasterLayer, QgsVectorLayer
        from gridforge.config import ColorMode, Units
        from gridforge.render.styles import STYLE_ROLES, apply_layer_style, get_qml_path

        gdal.UseExceptions()
        tif = Path({str(tmp_path / "hs.tif")!r})
        ds = gdal.GetDriverByName("GTiff").Create(str(tif), 4, 4, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        ds.GetRasterBand(1).Fill(190)
        ds = None

        slope_tif = Path({str(tmp_path / "slope.tif")!r})
        sds = gdal.GetDriverByName("GTiff").Create(str(slope_tif), 4, 4, 4, gdal.GDT_Byte)
        sds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        for idx_b, val in ((1, 229), (2, 62), (3, 62), (4, 185)):
            sds.GetRasterBand(idx_b).Fill(val)
        sds = None

        geom_by_role = {{
            "landcover": "MultiPolygon",
            "fema_flood": "MultiPolygon",
            "au_protected_areas": "MultiPolygon",
            "water_polygons": "MultiPolygon",
            "au_hydro_polys": "MultiPolygon",
            "contours": "LineString",
            "enc_depth_contours": "LineString",
            "nhd_flowlines": "LineString",
            "au_hydro_lines": "LineString",
            "waterways": "LineString",
            "buildings": "MultiPolygon",
            "building_icons": "MultiPolygon",
            "transport": "LineString",
            "au_state_trails": "LineString",
            "au_power_lines": "LineString",
            "au_pipelines": "LineString",
            "au_runways": "LineString",
            "points": "Point",
            "au_state_pois": "Point",
            "au_substations": "Point",
            "au_hydro_points": "Point",
            "au_homesteads": "Point",
            "au_airfields": "Point",
            "us_towers": "Point",
            "enc_navaids": "Point",
            "mgrs_grid_1km": "LineString",
            "mgrs_grid_100km": "LineString",
            "mgrs_zone_boundary": "LineString",
            "mgrs_square_labels": "Point",
            "overlay_polygons": "MultiPolygon",
            "overlay_lines": "LineString",
            "overlay_points": "Point",
            "waypoint_route": "LineString",
            "waypoint_points": "Point",
        }}

        for mode in (ColorMode.COLOR, ColorMode.BW):
            assert get_qml_path("hillshade", mode).is_file()
            rl = QgsRasterLayer(str(tif), "hs")
            assert apply_layer_style(rl, "hillshade", mode, prefer_qml=True) is True
            del rl

            assert get_qml_path("slope_shading", mode).is_file()
            sl = QgsRasterLayer(str(slope_tif), "slope")
            assert apply_layer_style(sl, "slope_shading", mode, prefer_qml=True) is True
            del sl

            for role in STYLE_ROLES:
                if role in ("hillshade", "slope_shading"):
                    continue
                assert get_qml_path(role, mode).is_file(), f"Missing QML: {{mode.value}}/{{role}}.qml"
                vl = QgsVectorLayer(
                    f"{{geom_by_role[role]}}?crs=EPSG:32756&field=name:string&field=is_index:integer&field=elev_label:string",
                    role,
                    "memory",
                )
                used_qml = apply_layer_style(vl, role, mode, units=Units.METRIC, scale=25000, prefer_qml=True)
                assert used_qml is True, f"Failed native QML load for {{mode.value}}/{{role}}"
                assert vl.renderer() is not None
                del vl
        """
    )


def test_carto_toggles_modify_rules_and_symbols(tmp_path: Path) -> None:
    _run_pyqgis_snippet(
        f"""
        from osgeo import gdal
        from qgis.core import QgsRasterLayer, QgsVectorLayer
        from gridforge.config import CartoOptions, ColorMode, DensityLevel, HillshadeStrength
        from gridforge.render.styles import apply_programmatic_style

        # 1. Trees toggle separates evergreen/deciduous/mixed when True and omits vegetation when False
        vl_lc_on = QgsVectorLayer(
            "MultiPolygon?crs=EPSG:32756&field=landuse:string&field=natural:string&field=leaf_type:string&field=wood:string",
            "lc_on",
            "memory",
        )
        apply_programmatic_style(vl_lc_on, "landcover", ColorMode.COLOR, carto=CartoOptions(show_trees=True))
        labels_on = {{r.label() for r in vl_lc_on.renderer().rootRule().children()}}
        assert "Evergreen / Coniferous Forest" in labels_on
        assert "Deciduous / Broadleaf Forest" in labels_on
        assert "Mixed / Unspecified Forest" in labels_on

        vl_lc_off = QgsVectorLayer(
            "MultiPolygon?crs=EPSG:32756&field=landuse:string&field=natural:string&field=leaf_type:string&field=wood:string",
            "lc_off",
            "memory",
        )
        apply_programmatic_style(vl_lc_off, "landcover", ColorMode.COLOR, carto=CartoOptions(show_trees=False))
        labels_off = {{r.label() for r in vl_lc_off.renderer().rootRule().children()}}
        assert "Evergreen / Coniferous Forest" not in labels_off
        assert "Deciduous / Broadleaf Forest" not in labels_off

        # 2. River downstream arrows toggle adds/removes QgsMarkerLineSymbolLayer
        vl_ww_on = QgsVectorLayer("LineString?crs=EPSG:32756&field=waterway:string&field=intermittent:string&field=name:string", "ww_on", "memory")
        apply_programmatic_style(vl_ww_on, "waterways", ColorMode.COLOR, carto=CartoOptions(show_river_arrows=True))
        river_rule_on = [r for r in vl_ww_on.renderer().rootRule().children() if r.label() == "Rivers & Canals"][0]
        assert river_rule_on.symbol().symbolLayerCount() == 2

        vl_ww_off = QgsVectorLayer("LineString?crs=EPSG:32756&field=waterway:string&field=intermittent:string&field=name:string", "ww_off", "memory")
        apply_programmatic_style(vl_ww_off, "waterways", ColorMode.COLOR, carto=CartoOptions(show_river_arrows=False))
        river_rule_off = [r for r in vl_ww_off.renderer().rootRule().children() if r.label() == "Rivers & Canals"][0]
        assert river_rule_off.symbol().symbolLayerCount() == 1

        # 3. Power & Pipeline toggles + LOW label density on transport
        vl_tr_off = QgsVectorLayer(
            "LineString?crs=EPSG:32756&field=highway:string&field=power:string&field=man_made:string&field=name:string&field=ref:string",
            "tr_off",
            "memory",
        )
        apply_programmatic_style(
            vl_tr_off,
            "transport",
            ColorMode.BW,
            carto=CartoOptions(show_power=False, show_pipelines=False, label_density=DensityLevel.LOW),
        )
        tr_labels = {{r.label() for r in vl_tr_off.renderer().rootRule().children()}}
        assert "Power Lines" not in tr_labels
        assert "Pipelines" not in tr_labels
        lbl_descs = {{r.description() for r in vl_tr_off.labeling().rootRule().children()}}
        assert "Minor Road & Trail Labels" not in lbl_descs

        # 4. Hillshade strength opacity progression (LIGHT < NORMAL < STRONG)
        gdal.UseExceptions()
        tif = Path({str(tmp_path / "hs2.tif")!r})
        ds = gdal.GetDriverByName("GTiff").Create(str(tif), 4, 4, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        ds.GetRasterBand(1).Fill(190)
        ds = None
        opacities = []
        for strength in (HillshadeStrength.LIGHT, HillshadeStrength.NORMAL, HillshadeStrength.STRONG):
            rl = QgsRasterLayer(str(tif), f"hs_{{strength.value}}")
            apply_programmatic_style(rl, "hillshade", ColorMode.COLOR, carto=CartoOptions(hillshade_strength=strength))
            opacities.append(rl.renderer().opacity())
            del rl
        assert opacities[0] < opacities[1] < opacities[2]
        """
    )


def test_peak_dedup_and_tower_unit_expressions() -> None:
    _run_pyqgis_snippet(
        """
        from qgis.core import (
            QgsExpression,
            QgsExpressionContext,
            QgsExpressionContextUtils,
            QgsFeature,
            QgsVectorLayer,
        )
        from gridforge.config import ColorMode, Units
        from gridforge.render.styles import apply_programmatic_style

        vl = QgsVectorLayer(
            "Point?crs=EPSG:32756&field=name:string&field=natural:string&field=ele:string"
            "&field=place:string&field=highway:string&field=ref:string&field=tourism:string"
            "&field=railway:string&field=amenity:string&field=building:string&field=emergency:string"
            "&field=man_made:string&field=tower:type:string&field=power:string&field=aeroway:string",
            "pts",
            "memory",
        )
        apply_programmatic_style(vl, "points", ColorMode.COLOR, units=Units.METRIC)
        peak_rule = [r for r in vl.labeling().rootRule().children() if r.description() == "Peaks & Elevations"][0]
        expr = QgsExpression(peak_rule.settings().fieldName)
        assert not expr.hasParserError(), expr.parserErrorString()

        for name, ele, expected in [
            ("Castle Cliff Trig (986 m)", "986", "Castle Cliff Trig (986 m)"),
            ("Mount Solitary", "950.4", "Mount Solitary\\n950 m"),
            ("Flagstaff Mountain", "2128 m", "Flagstaff Mountain\\n2128 m"),
        ]:
            f = QgsFeature(vl.fields())
            f.setAttribute("name", name)
            f.setAttribute("ele", ele)
            ctx = QgsExpressionContext()
            ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(vl))
            ctx.setFeature(f)
            assert expr.evaluate(ctx) == expected

        vl_tw = QgsVectorLayer(
            "Point?crs=EPSG:32613&field=source:string&field=name:string&field=obstacle_type:string"
            "&field=agl_ft:double&field=amsl_ft:double&field=lighting:string",
            "tw",
            "memory",
        )
        for units, expected_lbl in [(Units.IMPERIAL, "412 ft AGL"), (Units.METRIC, "126 m AGL")]:
            apply_programmatic_style(vl_tw, "us_towers", ColorMode.COLOR, units=units)
            tw_expr = QgsExpression(vl_tw.labeling().settings().fieldName)
            assert not tw_expr.hasParserError(), tw_expr.parserErrorString()
            f = QgsFeature(vl_tw.fields())
            f.setAttribute("agl_ft", 412.0)
            f.setAttribute("amsl_ft", 5800.0)
            ctx = QgsExpressionContext()
            ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(vl_tw))
            ctx.setFeature(f)
            assert tw_expr.evaluate(ctx) == expected_lbl
        """
    )


def test_au_overlay_styles_and_label_expressions() -> None:
    _run_pyqgis_snippet(
        """
        from qgis.core import (
            QgsExpression,
            QgsExpressionContext,
            QgsExpressionContextUtils,
            QgsFeature,
            QgsVectorLayer,
        )
        from gridforge.config import CartoOptions, ColorMode, DensityLevel
        from gridforge.render.styles import apply_programmatic_style

        def _eval(vl, expr_str, attrs):
            expr = QgsExpression(expr_str)
            assert not expr.hasParserError(), expr.parserErrorString()
            f = QgsFeature(vl.fields())
            for k, v in attrs.items():
                f.setAttribute(k, v)
            ctx = QgsExpressionContext()
            ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(vl))
            ctx.setFeature(f)
            return expr.evaluate(ctx)

        # 1. AU Hydro Points (Outback bores/waterholes deduplication)
        vl_hp = QgsVectorLayer(
            "Point?crs=EPSG:32752&field=name:string&field=featuretype:string&field=type:string&field=perenniality:string",
            "au_hp",
            "memory",
        )
        for mode in (ColorMode.COLOR, ColorMode.BW):
            apply_programmatic_style(vl_hp, "au_hydro_points", mode)
            expr_str = vl_hp.labeling().settings().fieldName
            assert _eval(vl_hp, expr_str, {"name": "Mutitjulu Waterhole", "featuretype": "Waterhole"}) == "Mutitjulu Waterhole"
            assert _eval(vl_hp, expr_str, {"name": "Currie", "featuretype": "Bore"}) == "Currie (Bore)"
            assert _eval(vl_hp, expr_str, {"name": "", "featuretype": "Bore"}) == "Bore"

        # 2. AU Protected Areas (CAPAD National Parks + IPA rules & uppercase label)
        vl_pk = QgsVectorLayer(
            "MultiPolygon?crs=EPSG:32752&field=name:string&field=park_type:string&field=type_abbr:string&field=iucn:string&field=state:string",
            "au_pk",
            "memory",
        )
        for mode in (ColorMode.COLOR, ColorMode.BW):
            apply_programmatic_style(vl_pk, "au_protected_areas", mode)
            rule_labels = {r.label() for r in vl_pk.renderer().rootRule().children()}
            assert "Indigenous Protected Areas (IPA)" in rule_labels
            assert "National Parks & Reserves (CAPAD)" in rule_labels
            lbl_rule = vl_pk.labeling().rootRule().children()[0]
            expr_str = lbl_rule.settings().fieldName
            assert _eval(vl_pk, expr_str, {"name": "Uluru-Kata Tjuta", "type_abbr": "NPC"}) == "ULURU-KATA TJUTA (NPC)"
            assert _eval(vl_pk, expr_str, {"name": "Katiti Petermann", "type_abbr": "IPA"}) == "KATITI PETERMANN (IPA)"

        # 3. AU Power Lines & Substations & Pipelines
        vl_pl = QgsVectorLayer(
            "LineString?crs=EPSG:32756&field=name:string&field=capacity_kv:double&field=status:string&field=state:string",
            "au_pl",
            "memory",
        )
        apply_programmatic_style(vl_pl, "au_power_lines", ColorMode.COLOR)
        pl_expr = vl_pl.labeling().settings().fieldName
        assert _eval(vl_pl, pl_expr, {"name": "Sydney West - Wallerawang", "capacity_kv": 330.0}) == "330 kV"
        assert _eval(vl_pl, pl_expr, {"name": "Feeder Line", "capacity_kv": None}) == "Feeder Line"

        vl_sub = QgsVectorLayer(
            "Point?crs=EPSG:32756&field=name:string&field=voltage_kv:double&field=status:string&field=state:string",
            "au_sub",
            "memory",
        )
        apply_programmatic_style(vl_sub, "au_substations", ColorMode.BW)
        sub_expr = vl_sub.labeling().settings().fieldName
        assert _eval(vl_sub, sub_expr, {"name": "Katoomba", "voltage_kv": 132.0}) == "Katoomba (132 kV)"

        vl_pipe = QgsVectorLayer(
            "LineString?crs=EPSG:32756&field=name:string&field=pipeline_type:string&field=status:string&field=operator:string",
            "au_pipe",
            "memory",
        )
        apply_programmatic_style(vl_pipe, "au_pipelines", ColorMode.COLOR)
        pipe_expr = vl_pipe.labeling().settings().fieldName
        assert _eval(vl_pipe, pipe_expr, {"name": "Moomba-Sydney", "pipeline_type": "gas"}) == "Moomba-Sydney"
        assert _eval(vl_pipe, pipe_expr, {"name": "", "pipeline_type": "gas"}) == "GAS PIPELINE"

        # 4. AU Hydro Lines river arrows toggle
        vl_hl_on = QgsVectorLayer(
            "LineString?crs=EPSG:32752&field=name:string&field=featuretype:string&field=perenniality:string&field=hierarchy:string",
            "au_hl_on",
            "memory",
        )
        apply_programmatic_style(vl_hl_on, "au_hydro_lines", ColorMode.COLOR, carto=CartoOptions(show_river_arrows=True))
        maj_on = [r for r in vl_hl_on.renderer().rootRule().children() if "Major" in r.label()][0]
        assert maj_on.symbol().symbolLayerCount() == 2

        vl_hl_off = QgsVectorLayer(
            "LineString?crs=EPSG:32752&field=name:string&field=featuretype:string&field=perenniality:string&field=hierarchy:string",
            "au_hl_off",
            "memory",
        )
        apply_programmatic_style(vl_hl_off, "au_hydro_lines", ColorMode.COLOR, carto=CartoOptions(show_river_arrows=False))
        maj_off = [r for r in vl_hl_off.renderer().rootRule().children() if "Major" in r.label()][0]
        assert maj_off.symbol().symbolLayerCount() == 1

        # 5. AU Airfields, Runways, Homesteads + LOW label density suppression
        vl_hs = QgsVectorLayer("Point?crs=EPSG:32752&field=name:string&field=featuretype:string", "au_hs", "memory")
        apply_programmatic_style(vl_hs, "au_homesteads", ColorMode.COLOR, carto=CartoOptions(label_density=DensityLevel.LOW))
        assert vl_hs.labelsEnabled() is False
        apply_programmatic_style(vl_hs, "au_homesteads", ColorMode.BW, carto=CartoOptions(label_density=DensityLevel.NORMAL))
        assert vl_hs.labelsEnabled() is True
        """
    )


def test_runner_dem_au_elvis_and_offline_pack_airgapped(tmp_path: Path) -> None:
    # 1. Verify DemSourceKind.AU_ELVIS resolves directly to AUElvisDEMSource
    au_spec = SheetSpec(
        center_lat=-33.732,
        center_lon=150.312,
        scale=25000,
        dem_source=DemSourceKind.AU_ELVIS,
    )
    assert isinstance(_resolve_elevation_source(au_spec, None), AUElvisDEMSource)

    # 2. Create a minimal self-contained synthetic offline pack on disk (zero network!)
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    ogr.UseExceptions()

    pack_dir = tmp_path / "mini-au-pack"
    overlays_dir = pack_dir / "overlays"
    overlays_dir.mkdir(parents=True)

    # Synthetic GeoTIFF covering Katoomba (-33.732, 150.312) in WGS84
    dem_path = pack_dir / "dem.tif"
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(dem_path), 64, 64, 1, gdal.GDT_Float32)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((150.20, 0.25 / 64.0, 0.0, -33.62, 0.0, -0.25 / 64.0))
    ds.GetRasterBand(1).Fill(950.0)
    ds = None

    # Minimal valid OSM XML clipped via LocalPBFSource -> osmium
    osm_src = tmp_path / "mini.osm"
    osm_src.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osm version="0.6" generator="gridforge-test">\n'
        '  <node id="1" lat="-33.732" lon="150.312" version="1">\n'
        '    <tag k="name" v="Katoomba Test Peak"/>\n'
        '    <tag k="natural" v="peak"/>\n'
        '    <tag k="ele" v="1010"/>\n'
        '  </node>\n'
        '</osm>\n',
        encoding="utf-8",
    )
    pbf_path = pack_dir / "osm.osm.pbf"
    subprocess.run(["osmium", "cat", str(osm_src), "-o", str(pbf_path), "--overwrite"], check=True)

    # Minimal WGS84 AU Hydro GeoPackage with one bore point
    hydro_gpkg = overlays_dir / "au_hydro.gpkg"
    gpkg_drv = ogr.GetDriverByName("GPKG")
    gds = gpkg_drv.CreateDataSource(str(hydro_gpkg))
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = gds.CreateLayer("au_hydro_points", srs=wgs84, geom_type=ogr.wkbPoint)
    for fname in ("name", "featuretype", "type", "perenniality"):
        lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    feat = ogr.Feature(lyr.GetLayerDefn())
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(150.312, -33.732)
    feat.SetGeometry(pt)
    feat.SetField("name", "Blue Mtn Spring")
    feat.SetField("featuretype", "Spring")
    feat.SetField("type", "Natural")
    feat.SetField("perenniality", "Perennial")
    lyr.CreateFeature(feat)
    feat = None
    gds = None

    manifest = PackManifest(
        pack_id="mini-blue-mountains",
        name="Mini Blue Mountains Test Pack",
        region="blue-mountains",
        bbox_wgs84=BBox(min_x=150.20, min_y=-33.87, max_x=150.45, max_y=-33.62),
        utm_zones=["56H"],
        dem_path="dem.tif",
        vertical_datum="AHD",
        dem_attribution="Elevation data: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)",
        osm_pbf_path="osm.osm.pbf",
        overlays={"au-hydro": "overlays/au_hydro.gpkg"},
        attributions=[],
        created_at="2026-11-26T00:00:00Z",
        size_bytes=1024,
    )
    (pack_dir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    captured_ws = []
    progress_events: list[tuple[str, int]] = []

    class _DummyRenderer:
        def render_pdf(self, workspace, output_pdf: Path) -> RenderResult:
            captured_ws.append(workspace)
            output_pdf.parent.mkdir(parents=True, exist_ok=True)
            output_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            return RenderResult(
                output_pdf=output_pdf,
                page_width_mm=workspace.layout.page_width_mm,
                page_height_mm=workspace.layout.page_height_mm,
                scale=workspace.layout.spec.scale,
                utm_epsg=workspace.layout.utm_epsg,
                elapsed_seconds=0.05,
            )

    spec = SheetSpec(
        center_lat=-33.732,
        center_lon=150.312,
        scale=25000,
        color_mode=ColorMode.COLOR,
    )
    out_pdf = tmp_path / "out.pdf"
    res = run_render_job(
        spec,
        out_pdf,
        pack_dir=pack_dir,
        progress_callback=lambda stage, pct: progress_events.append((stage, pct)),
        renderer=_DummyRenderer(),
    )

    assert res.output_pdf == out_pdf
    assert len(captured_ws) == 1
    ws = captured_ws[0]
    assert ws.vertical_datum == "AHD"
    assert ws.dem_source_name == "pack:mini-blue-mountains"
    assert "au-hydro" in ws.regional_overlays
    assert any("Geoscience Australia" in a for a in ws.attributions)
    assert any("Surface Hydrology" in a for a in ws.attributions)

    # Progress events must be non-empty, strictly monotonic, and terminate at ("done", 100)
    assert progress_events[0][0] == "fetching_data"
    assert progress_events[-1] == ("done", 100)
    pcts = [pct for _, pct in progress_events]
    assert pcts == sorted(pcts)
    assert len(set(pcts)) == len(pcts)

    # Requesting an overlay NOT bundled in the pack stays strictly air-gapped (skipped cleanly)
    captured_ws.clear()
    spec_missing_ov = spec.model_copy(update={"au_overlays": [AUOverlayKind.AU_PARKS]})
    run_render_job(
        spec_missing_ov,
        tmp_path / "out2.pdf",
        pack_dir=pack_dir,
        renderer=_DummyRenderer(),
    )
    assert captured_ws[0].regional_overlays == {}


def test_v11_slope_state_trails_waypoints_styles_and_cli_parsing(tmp_path: Path) -> None:
    from gridforge.cli import _parse_waypoints
    from gridforge.config import WaypointCategory

    wps = _parse_waypoints(
        [
            "Katoomba Base,-33.7190,150.3114,camp",
            "Echo Point,-33.7322,150.3117",
            "Steep Gully,-33.7350,150.3150,hazard,Watch loose rock",
        ]
    )
    assert len(wps) == 3
    assert wps[0].category == WaypointCategory.CAMP
    assert wps[1].category == WaypointCategory.WAYPOINT
    assert wps[2].category == WaypointCategory.HAZARD
    assert wps[2].notes == "Watch loose rock"

    _run_pyqgis_snippet(
        f"""
        from osgeo import gdal
        from qgis.core import (
            QgsExpression,
            QgsExpressionContext,
            QgsExpressionContextUtils,
            QgsFeature,
            QgsRasterLayer,
            QgsVectorLayer,
        )
        from qgis.PyQt.QtGui import QPainter
        from gridforge.config import ColorMode, Units
        from gridforge.render.layout import _split_base_and_vector_layers
        from gridforge.render.styles import apply_programmatic_style

        def _eval(vl, expr_str, attrs):
            expr = QgsExpression(expr_str)
            assert not expr.hasParserError(), expr.parserErrorString()
            f = QgsFeature(vl.fields())
            for k, v in attrs.items():
                f.setAttribute(k, v)
            ctx = QgsExpressionContext()
            ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(vl))
            ctx.setFeature(f)
            return expr.evaluate(ctx)

        # 1. Slope-angle shading stays on bottom raster stack (with BlendNormal) while vectors stay pure
        gdal.UseExceptions()
        slope_tif = Path({str(tmp_path / "slope_rgba.tif")!r})
        sds = gdal.GetDriverByName("GTiff").Create(str(slope_tif), 4, 4, 4, gdal.GDT_Byte)
        sds.SetGeoTransform((250000.0, 10.0, 0.0, 6265000.0, 0.0, -10.0))
        for b, v in ((1, 237), (2, 137), (3, 54), (4, 155)):
            sds.GetRasterBand(b).Fill(v)
        sds = None

        sl = QgsRasterLayer(str(slope_tif), "Slope Shading")
        apply_programmatic_style(sl, "slope_shading", ColorMode.COLOR)
        assert sl.blendMode() == QPainter.CompositionMode_SourceOver
        assert 0.35 <= sl.renderer().opacity() <= 0.70

        vl_top = QgsVectorLayer("LineString?crs=EPSG:32756&field=name:string", "Contours", "memory")
        vl_bot = QgsVectorLayer("MultiPolygon?crs=EPSG:32756&field=name:string", "Landcover", "memory")
        top_lyrs, bot_lyrs = _split_base_and_vector_layers([vl_top, sl, vl_bot])
        assert top_lyrs == [vl_top]
        assert bot_lyrs == [sl, vl_bot]
        del sl, vl_top, vl_bot

        # 2. NSW / VIC state trails rules & fire-trail (FT) label suffix dedup
        vl_tr = QgsVectorLayer(
            "LineString?crs=EPSG:32756&field=name:string&field=trail_class:string&field=surface:string&field=state:string",
            "au_state_trails",
            "memory",
        )
        for mode in (ColorMode.COLOR, ColorMode.BW):
            apply_programmatic_style(vl_tr, "au_state_trails", mode)
            labels = {{r.label() for r in vl_tr.renderer().rootRule().children()}}
            assert "Fire & Management Trails" in labels
            assert "4WD Tracks" in labels
            assert "Walking Tracks" in labels
            ft_lbl = [r for r in vl_tr.labeling().rootRule().children() if r.description() == "Fire Trail Labels"][0]
            expr_s = ft_lbl.settings().fieldName
            assert _eval(vl_tr, expr_s, {{"name": "Narrow Neck Fire Trail", "trail_class": "fire_trail"}}) == "Narrow Neck Fire Trail"
            assert _eval(vl_tr, expr_s, {{"name": "Boyd", "trail_class": "fire_trail"}}) == "Boyd (FT)"

        # 3. NSW / VIC state POIs (huts, campgrounds, trigs) + metric/imperial trig elevation
        vl_poi = QgsVectorLayer(
            "Point?crs=EPSG:32755&field=name:string&field=poi_type:string&field=elev_m:double&field=state:string",
            "au_state_pois",
            "memory",
        )
        for units, expected_trig in [
            (Units.METRIC, "KOSCIUSZKO\\n2229 m"),
            (Units.IMPERIAL, "KOSCIUSZKO\\n7314 ft"),
        ]:
            apply_programmatic_style(vl_poi, "au_state_pois", ColorMode.COLOR, units=units)
            trig_rule = [r for r in vl_poi.labeling().rootRule().children() if r.description() == "Trig Station Labels"][0]
            got = _eval(vl_poi, trig_rule.settings().fieldName, {{"name": "KOSCIUSZKO", "poi_type": "trig", "elev_m": 2229.4}})
            assert got == expected_trig, f"got={{got!r}} expected={{expected_trig!r}}"

        # 4. Waypoints badge rules & WP1 · Label expression
        vl_wp = QgsVectorLayer(
            "Point?crs=EPSG:32756&field=seq:integer&field=code:string&field=label:string&field=category:string&field=mgrs_10m:string&field=elev_m:double",
            "waypoint_points",
            "memory",
        )
        for mode in (ColorMode.COLOR, ColorMode.BW):
            apply_programmatic_style(vl_wp, "waypoint_points", mode)
            wp_expr = vl_wp.labeling().settings().fieldName
            assert _eval(vl_wp, wp_expr, {{"code": "WP1", "label": "Echo Point"}}) == "WP1 · Echo Point"
            assert _eval(vl_wp, wp_expr, {{"code": "WP1", "label": "WP1"}}) == "WP1"
        """
    )


def test_v12_pan_australia_all_state_trail_keys_and_dynamic_attributions(
    tmp_path: Path, monkeypatch
) -> None:
    from gridforge.config import (
        AU_STATE_TRAIL_OVERLAY_KINDS,
        DeclinationInfo,
        JobWorkspace,
        MGRSGridMeta,
    )
    from gridforge.geo.crs import compute_sheet_layout
    from gridforge.pipeline import runner
    from gridforge.render.layout import _AU_STATE_TRAIL_KEYS

    # 1. All 9 canonical state-trail kinds (auto + 8 states/territories) must be recognized by layout Map Key
    assert len(AU_STATE_TRAIL_OVERLAY_KINDS) == 9
    expected_keys = {
        "au-state-trails",
        "au-nsw-trails",
        "au-vic-trails",
        "au-tas-trails",
        "au-qld-trails",
        "au-wa-trails",
        "au-sa-trails",
        "au-nt-trails",
        "au-act-trails",
    }
    assert {k.value for k in AU_STATE_TRAIL_OVERLAY_KINDS} == expected_keys
    for key in expected_keys:
        assert key in _AU_STATE_TRAIL_KEYS
        assert key.replace("-", "_") in _AU_STATE_TRAIL_KEYS

    # 2. _fetch_regional_overlays must deduplicate dynamic resolved_attributions (cross-border Auto router)
    dummy_gpkg = tmp_path / "au_state_trails.gpkg"
    dummy_gpkg.write_bytes(b"placeholder")

    class _MockAutoStateSource:
        attribution = "Fallback attribution (should be ignored when resolved_attributions is set)"
        resolved_attributions = [
            "NSW Trails: © State of NSW (CC BY 4.0)",
            "ACT Trails: © ACT Government (CC BY 4.0)",
            "NSW Trails: © State of NSW (CC BY 4.0)",
        ]

        def fetch_overlay(self, layout, out_dir: Path) -> Path:
            return dummy_gpkg

    class _MockFallbackSource:
        attribution = "Victoria Trails: © State of Victoria (CC BY 4.0)"

        def fetch_overlay(self, layout, out_dir: Path) -> Path:
            return dummy_gpkg

    spec = SheetSpec(
        center_lat=-35.640,
        center_lon=148.980,
        scale=25000,
        au_overlays=[AUOverlayKind.AU_STATE_TRAILS, AUOverlayKind.AU_VIC_TRAILS],
    )
    layout = compute_sheet_layout(spec)

    monkeypatch.setattr(
        runner,
        "build_au_overlay_sources",
        lambda kinds, local_overrides=None: {
            "au-state-trails": _MockAutoStateSource(),
            "au-vic-trails": _MockFallbackSource(),
        },
    )
    paths, attrs = runner._fetch_regional_overlays(spec, layout, tmp_path)
    assert set(paths.keys()) == {"au-state-trails", "au-vic-trails"}
    assert attrs == [
        "NSW Trails: © State of NSW (CC BY 4.0)",
        "ACT Trails: © ACT Government (CC BY 4.0)",
        "Victoria Trails: © State of Victoria (CC BY 4.0)",
    ]

    # 3. Verify _resolve_pack_sources maps bundled au-state-trails cleanly (auto & specific state alias)
    pack_dir = tmp_path / "tas-pack"
    ov_dir = pack_dir / "overlays"
    ov_dir.mkdir(parents=True)
    bundled = ov_dir / "au_state_trails.gpkg"
    bundled.write_bytes(b"gpkg")
    manifest = PackManifest(
        pack_id="tas",
        name="Tasmania Test Pack",
        region="tas",
        bbox_wgs84=BBox(min_x=144.0, min_y=-43.5, max_x=148.5, max_y=-40.0),
        utm_zones=["55G"],
        overlays={"au-state-trails": "overlays/au_state_trails.gpkg"},
        attributions=[],
        created_at="2026-11-26T00:00:00Z",
        size_bytes=512,
    )
    (pack_dir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    tas_auto_spec = SheetSpec(center_lat=-41.684, center_lon=145.951, scale=25000)
    tas_layout = compute_sheet_layout(tas_auto_spec)
    _, _, _, _, overrides_auto, _, au_kinds_auto = runner._resolve_pack_sources(
        pack_dir,
        tas_auto_spec,
        tas_layout,
        elevation_source=None,
        vector_source=None,
        regional_overlay_overrides=None,
    )
    assert au_kinds_auto == [AUOverlayKind.AU_STATE_TRAILS]
    assert overrides_auto["au-state-trails"] == bundled.resolve()

    tas_explicit_spec = tas_auto_spec.model_copy(
        update={"au_overlays": [AUOverlayKind.AU_TAS_TRAILS]}
    )
    _, _, _, _, overrides_tas, _, au_kinds_tas = runner._resolve_pack_sources(
        pack_dir,
        tas_explicit_spec,
        tas_layout,
        elevation_source=None,
        vector_source=None,
        regional_overlay_overrides=None,
    )
    assert au_kinds_tas == [AUOverlayKind.AU_TAS_TRAILS]
    assert overrides_tas["au-tas-trails"] == bundled.resolve()

    # 4. Verify populate_project_layers loads au_state_trails & au_state_pois for ALL 9 keys (Color & B&W)
    from osgeo import ogr, osr

    ogr.UseExceptions()
    osr.UseExceptions()
    state_gpkg = tmp_path / "state_sample.gpkg"
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32755)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(str(state_gpkg))
    lt = ds.CreateLayer("au_state_trails", srs=srs, geom_type=ogr.wkbMultiLineString)
    for fn in ("name", "trail_class", "surface", "state"):
        lt.CreateField(ogr.FieldDefn(fn, ogr.OFTString))
    lp = ds.CreateLayer("au_state_pois", srs=srs, geom_type=ogr.wkbPoint)
    lp.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lp.CreateField(ogr.FieldDefn("poi_type", ogr.OFTString))
    lp.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
    lp.CreateField(ogr.FieldDefn("state", ogr.OFTString))
    ds = None

    # Minimal empty placeholder files required by JobWorkspace model
    empty_osm = tmp_path / "empty_osm.gpkg"
    ds2 = drv.CreateDataSource(str(empty_osm))
    ds2.CreateLayer("lines", srs=srs, geom_type=ogr.wkbLineString)
    ds2 = None

    ws_base = JobWorkspace(
        job_dir=tmp_path,
        layout=tas_layout,
        declination=DeclinationInfo(
            decimal_year=2026.5,
            magnetic_declination_deg=13.5,
            annual_change_deg=0.05,
            grid_convergence_deg=-0.5,
            gm_angle_deg=14.0,
            gm_angle_mils=248.9,
            grid_convergence_mils=-8.9,
        ),
        mgrs_meta=MGRSGridMeta(
            gzd="55G",
            square_ids_100km=["DP"],
            center_mgrs_10m="55G DP 1234 5678",
            grid_spacing_mm=40.0,
            crosses_100km_boundary=False,
            crosses_utm_zone_boundary=False,
            ticks=[],
        ),
        osm_gpkg=empty_osm,
        terrain_gpkg=empty_osm,
        hillshade_tif=tmp_path / "missing_hs.tif",
        dem_utm_tif=tmp_path / "missing_dem.tif",
        mgrs_gpkg=empty_osm,
        declination_svg=tmp_path / "decl.svg",
        grid_ref_svg=tmp_path / "grid.svg",
        contour_interval_display="10 m (index 50 m)",
        attributions=[],
    )
    ws_json = ws_base.model_dump_json()

    _run_pyqgis_snippet(
        f"""
        from qgis.core import QgsProject
        from gridforge.config import AU_STATE_TRAIL_OVERLAY_KINDS, ColorMode, JobWorkspace
        from gridforge.render.styles import populate_project_layers

        base_ws = JobWorkspace.model_validate_json({ws_json!r})
        gpkg = Path({str(state_gpkg)!r})

        for mode in (ColorMode.COLOR, ColorMode.BW):
            spec_m = base_ws.layout.spec.model_copy(update={{"color_mode": mode}})
            layout_m = base_ws.layout.model_copy(update={{"spec": spec_m}})
            for kind in AU_STATE_TRAIL_OVERLAY_KINDS:
                ws = base_ws.model_copy(update={{"layout": layout_m, "regional_overlays": {{kind.value: gpkg}}}})
                proj = QgsProject.instance()
                proj.clear()
                layers = populate_project_layers(proj, ws, include_grid=False)
                names = [l.name() for l in layers]
                assert any("Huts, Camps & Trigs" in n for n in names), f"Missing POIs layer for {{kind.value}} ({{mode.value}}): {{names}}"
                assert any("Fire Trails & Tracks" in n for n in names), f"Missing Trails layer for {{kind.value}} ({{mode.value}}): {{names}}"
                layers.clear()
                proj.clear()
        """
    )
