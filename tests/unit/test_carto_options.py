"""Unit tests for Phase 2 military cartography, icons, CartoOptions toggles, and regional styling.

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

from gridforge.render.styles import (
    get_icon_path,
    get_icons_root,
    scale_font_delta,
    scale_width_factor,
)


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
    )
    root = get_icons_root()
    assert root.is_dir()
    for name in expected:
        p = get_icon_path(name, icons_root=root)
        assert p.is_file(), f"Missing icon: {p}"
        elem = ET.fromstring(p.read_text(encoding="utf-8"))
        assert elem.tag.endswith("svg")


def test_multiscale_factors() -> None:
    assert scale_width_factor(15000) > scale_width_factor(25000) > scale_width_factor(50000)
    assert scale_font_delta(15000) > scale_font_delta(25000) > scale_font_delta(50000)


def test_all_qml_styles_roundtrip(tmp_path: Path) -> None:
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

        geom_by_role = {{
            "landcover": "MultiPolygon",
            "fema_flood": "MultiPolygon",
            "water_polygons": "MultiPolygon",
            "contours": "LineString",
            "enc_depth_contours": "LineString",
            "nhd_flowlines": "LineString",
            "waterways": "LineString",
            "buildings": "MultiPolygon",
            "building_icons": "MultiPolygon",
            "transport": "LineString",
            "points": "Point",
            "us_towers": "Point",
            "enc_navaids": "Point",
            "mgrs_grid_1km": "LineString",
            "mgrs_grid_100km": "LineString",
            "mgrs_zone_boundary": "LineString",
            "mgrs_square_labels": "Point",
            "overlay_polygons": "MultiPolygon",
            "overlay_lines": "LineString",
            "overlay_points": "Point",
        }}

        for mode in (ColorMode.COLOR, ColorMode.BW):
            assert get_qml_path("hillshade", mode).is_file()
            rl = QgsRasterLayer(str(tif), "hs")
            assert apply_layer_style(rl, "hillshade", mode, prefer_qml=True) is True
            del rl

            for role in STYLE_ROLES:
                if role == "hillshade":
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
