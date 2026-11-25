"""Isolated PyQGIS 3.40 subprocess renderer implementing ``MapRenderer``.

Every render runs in a fresh spawned Python subprocess (never ``fork()``) so
Qt/QGIS C++ memory is completely reclaimed on exit with zero leak accumulation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from gridforge.config import JobWorkspace, RenderResult

logger = logging.getLogger(__name__)


def _list_gpkg_layers(gpkg_path: Path) -> list[str]:
    """List vector layer names present in a GeoPackage using OGR."""
    from osgeo import ogr

    ogr.UseExceptions()
    ds = ogr.Open(str(gpkg_path))
    if ds is None:
        return []
    try:
        return [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    finally:
        ds = None


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

    from gridforge.render.styles import apply_layer_style

    if not gpkg_path.is_file():
        return None
    uri = f"{gpkg_path}|layername={layer_name}"
    vlayer = QgsVectorLayer(uri, display_name, "ogr")
    if not vlayer.isValid():
        logger.debug("Skipping unavailable vector layer %s (%s)", display_name, uri)
        return None

    apply_layer_style(
        vlayer,
        role,
        workspace.layout.spec.color_mode,
        units=workspace.layout.spec.units,
    )
    project.addMapLayer(vlayer, False)
    return vlayer


def _load_overlay_layers(project: Any, workspace: JobWorkspace) -> list[Any]:
    """Load optional user overlay GeoPackage layers ordered Points -> Lines -> Polygons."""
    from qgis.core import Qgis, QgsVectorLayer, QgsWkbTypes

    from gridforge.render.styles import apply_layer_style

    if workspace.overlays_gpkg is None or not workspace.overlays_gpkg.is_file():
        return []

    layer_names = _list_gpkg_layers(workspace.overlays_gpkg)
    points_layers: list[Any] = []
    lines_layers: list[Any] = []
    poly_layers: list[Any] = []

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
            workspace.layout.spec.color_mode,
            units=workspace.layout.spec.units,
        )
        project.addMapLayer(vlayer, False)
        target_bucket.append(vlayer)

    return points_layers + lines_layers + poly_layers


def _assemble_project_layers(project: Any, workspace: JobWorkspace) -> list[Any]:
    """Load and style all workspace layers in exact top-to-bottom QGIS map order.

    Index 0 is drawn LAST (on top); last index is drawn FIRST (bottom).
    Hillshade (Multiply) sits immediately above landcover polygons so every
    layer above hillshade renders as 100% crisp un-rasterized vector graphics.
    """
    from qgis.core import QgsCoordinateReferenceSystem, QgsRasterLayer

    from gridforge.render.styles import apply_layer_style

    project.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{workspace.layout.utm_epsg}"))

    osm_layers = set(_list_gpkg_layers(workspace.osm_gpkg)) if workspace.osm_gpkg.is_file() else set()
    mgrs_layers = set(_list_gpkg_layers(workspace.mgrs_gpkg)) if workspace.mgrs_gpkg.is_file() else set()
    terrain_layers = set(_list_gpkg_layers(workspace.terrain_gpkg)) if workspace.terrain_gpkg.is_file() else set()

    ordered_top_to_bottom: list[Any] = []

    def _push(layer: Any | None) -> None:
        if layer is not None:
            ordered_top_to_bottom.append(layer)

    # 1. User overlays on very top
    ordered_top_to_bottom.extend(_load_overlay_layers(project, workspace))

    # 2. MGRS Grid stack (100% pure vector, BlendNormal)
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

    # 3. OSM Points (Peaks, Places, Stations, Towers)
    if "points" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "points", "Points & Places", role="points", workspace=workspace))

    # 4. OSM Transport & Infrastructure (Roads, Trails, Railways, Power)
    if "lines" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "lines", "Roads & Transport", role="transport", workspace=workspace))

    # 5. OSM Buildings
    if "multipolygons" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "multipolygons", "Buildings", role="buildings", workspace=workspace))

    # 6. OSM Waterways (Rivers, Streams, Intermittent)
    if "lines" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "lines", "Waterways", role="waterways", workspace=workspace))

    # 7. Elevation Contours (Index + Minor with curved haloed labels)
    if "contours" in terrain_layers:
        _push(_load_vector_layer(project, workspace.terrain_gpkg, "contours", "Contours", role="contours", workspace=workspace))

    # 8. OSM Water Polygons (above hillshade so water stays crisp and clean)
    if "multipolygons" in osm_layers:
        _push(
            _load_vector_layer(
                project, workspace.osm_gpkg, "multipolygons", "Waterbodies", role="water_polygons", workspace=workspace
            )
        )

    # 9. Multi-directional Hillshade (Multiply blend over landcover + background)
    if workspace.hillshade_tif.is_file():
        hs_layer = QgsRasterLayer(str(workspace.hillshade_tif), "Hillshade")
        if hs_layer.isValid():
            apply_layer_style(
                hs_layer,
                "hillshade",
                workspace.layout.spec.color_mode,
                units=workspace.layout.spec.units,
            )
            project.addMapLayer(hs_layer, False)
            ordered_top_to_bottom.append(hs_layer)

    # 10. OSM Landcover Polygons (bottom layer — receives Multiply hillshade!)
    if "multipolygons" in osm_layers:
        _push(_load_vector_layer(project, workspace.osm_gpkg, "multipolygons", "Landcover", role="landcover", workspace=workspace))

    return ordered_top_to_bottom


def render_workspace_in_process(workspace: JobWorkspace, output_pdf: Path) -> RenderResult:
    """Execute PyQGIS rendering inside the current process (called by worker subprocess)."""
    import gc

    from qgis.core import QgsApplication, QgsProject

    from gridforge.render.layout import build_print_layout, export_layout_to_pdf

    t0 = time.perf_counter()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    runtime_dir = Path(os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root"))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        runtime_dir.chmod(0o700)
    except OSError:
        pass

    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    qgs = QgsApplication([], False)
    qgs.initQgis()

    def _execute_render() -> Path:
        project = QgsProject.instance()
        project.clear()
        ordered_layers = _assemble_project_layers(project, workspace)
        print_layout, map_item = build_print_layout(project, workspace, ordered_layers)

        # Verify physical scale lock before exporting
        actual_scale = map_item.scale()
        expected_scale = float(workspace.layout.spec.scale)
        rel_err = abs(actual_scale - expected_scale) / expected_scale
        if rel_err > 1e-4:
            raise RuntimeError(
                f"Map scale drift detected before export: actual={actual_scale:.4f}, expected={expected_scale:.4f}"
            )

        exported = export_layout_to_pdf(
            print_layout,
            output_pdf,
            dpi=workspace.layout.spec.dpi,
            geopdf=workspace.layout.spec.geopdf,
        )

        # Explicitly release Qt/QGIS objects before project.clear() and exitQgis()
        del map_item
        del print_layout
        ordered_layers.clear()
        del ordered_layers
        project.clear()
        return exported

    try:
        out_path = _execute_render()
    finally:
        gc.collect()
        qgs.exitQgis()

    elapsed = round(time.perf_counter() - t0, 3)
    return RenderResult(
        output_pdf=out_path,
        page_width_mm=workspace.layout.page_width_mm,
        page_height_mm=workspace.layout.page_height_mm,
        scale=workspace.layout.spec.scale,
        utm_epsg=workspace.layout.utm_epsg,
        elapsed_seconds=elapsed,
        workspace_dir=workspace.job_dir,
    )


class PyQGISRenderer:
    """``MapRenderer`` implementation isolating PyQGIS inside a spawned subprocess."""

    def __init__(self, *, timeout_seconds: int = 300, use_xvfb: bool | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.use_xvfb = use_xvfb

    def render_pdf(self, workspace: JobWorkspace, output_pdf: Path) -> RenderResult:
        """Spawn a fresh PyQGIS worker subprocess to render ``workspace`` to ``output_pdf``."""
        caller_output_pdf = Path(output_pdf)
        resolved_output_pdf = caller_output_pdf.resolve()
        resolved_output_pdf.parent.mkdir(parents=True, exist_ok=True)

        workspace.job_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = workspace.job_dir / "render_manifest.json"
        result_path = workspace.job_dir / "render_result.json"
        manifest_path.write_text(workspace.model_dump_json(indent=2), encoding="utf-8")

        use_xvfb = (
            self.use_xvfb
            if self.use_xvfb is not None
            else os.environ.get("GRIDFORGE_USE_XVFB", "").strip().lower() in ("1", "true", "yes")
        )

        cmd: list[str] = []
        if use_xvfb:
            cmd.extend(["xvfb-run", "-a"])
        cmd.extend(
            [
                sys.executable,
                "-m",
                "gridforge.render.pyqgis_worker",
                str(manifest_path),
                str(resolved_output_pdf),
                "--result-json",
                str(result_path),
            ]
        )

        env = os.environ.copy()
        if use_xvfb:
            env["QT_QPA_PLATFORM"] = "xcb"
        else:
            env.setdefault("QT_QPA_PLATFORM", "offscreen")

        runtime_dir = Path(env.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root"))
        runtime_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"PyQGIS worker subprocess failed (exit={proc.returncode}).\n"
                f"Command: {' '.join(cmd)}\n"
                f"STDERR:\n{proc.stderr[-3000:]}\n"
                f"STDOUT:\n{proc.stdout[-1500:]}"
            )

        parsed: RenderResult | None = None
        if result_path.is_file():
            parsed = RenderResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        else:
            # Fallback: parse last JSON line from stdout
            for line in reversed(proc.stdout.splitlines()):
                line_s = line.strip()
                if line_s.startswith("{") and line_s.endswith("}"):
                    parsed = RenderResult.model_validate_json(line_s)
                    break

        if parsed is None:
            raise RuntimeError(
                f"PyQGIS worker exited 0 but did not produce RenderResult JSON.\nSTDOUT:\n{proc.stdout}"
            )

        return parsed.model_copy(update={"output_pdf": caller_output_pdf})


def _worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GridForge isolated PyQGIS render worker")
    parser.add_argument("manifest_json", type=Path, help="Serialized JobWorkspace JSON")
    parser.add_argument("output_pdf", type=Path, help="Target PDF output path")
    parser.add_argument("--result-json", type=Path, default=None, help="Optional path to write RenderResult JSON")
    args = parser.parse_args(argv)

    workspace = JobWorkspace.model_validate_json(args.manifest_json.read_text(encoding="utf-8"))
    result = render_workspace_in_process(workspace, args.output_pdf)
    payload = result.model_dump_json()

    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(payload, encoding="utf-8")

    # Also print compact JSON to stdout for callers reading stdout directly
    print(json.dumps(json.loads(payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
