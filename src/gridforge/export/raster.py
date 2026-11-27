"""Collarless true-scale UTM GeoTIFF rendering via isolated PyQGIS subprocess.

Produces an RGB GeoTIFF covering ``workspace.layout.utm_bbox`` using the exact same
PyQGIS 3.40 layer assembly and cartographic styling as print PDF rendering, with no
collar or margins, ready for Web Mercator (MBTiles) or WGS84 (Garmin KMZ) export.
"""

from __future__ import annotations

import argparse
import gc
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from gridforge.config import JobWorkspace

# Cap extreme accidental pixel dimensions while preserving exact aspect ratio.
MAX_RASTER_DIM_PX: int = 16384


def compute_collarless_dimensions(
    frame_width_mm: float,
    frame_height_mm: float,
    dpi: int,
    max_dim_px: int = MAX_RASTER_DIM_PX,
) -> tuple[int, int]:
    """Compute exact (width_px, height_px) for the collarless map body at ``dpi``."""
    if dpi <= 0:
        raise ValueError(f"dpi must be positive, got {dpi}.")
    if frame_width_mm <= 0.0 or frame_height_mm <= 0.0:
        raise ValueError(
            f"Map frame dimensions must be positive, got {frame_width_mm} x {frame_height_mm} mm."
        )

    width_px = max(1, round(frame_width_mm / 25.4 * dpi))
    height_px = max(1, round(frame_height_mm / 25.4 * dpi))

    peak = max(width_px, height_px)
    if peak > max_dim_px:
        scale = max_dim_px / peak
        width_px = max(1, round(width_px * scale))
        height_px = max(1, round(height_px * scale))

    return (width_px, height_px)


def _is_mgrs_layer(layer: Any, mgrs_gpkg: Path) -> bool:
    """Return True if a QGIS map layer belongs to the MGRS grid GeoPackage."""
    name = (layer.name() or "").strip()
    if name.startswith("MGRS ") or name == "UTM Zone Boundary":
        return True
    try:
        src = str(layer.source() or "")
    except Exception:
        src = ""
    if str(mgrs_gpkg) in src or mgrs_gpkg.name in src:
        return True
    return False


def _populate_layers_for_export(
    project: Any,
    workspace: JobWorkspace,
    *,
    include_grid: bool,
) -> list[Any]:
    """Populate styled PyQGIS layers via styles.populate_project_layers (or worker fallback)."""
    from gridforge.render import pyqgis_worker, styles

    effective_workspace = workspace
    if not include_grid and workspace.layout.spec.carto.show_grid:
        updated_carto = workspace.layout.spec.carto.model_copy(update={"show_grid": False})
        updated_spec = workspace.layout.spec.model_copy(update={"carto": updated_carto})
        updated_layout = workspace.layout.model_copy(update={"spec": updated_spec})
        effective_workspace = workspace.model_copy(update={"layout": updated_layout})

    effective_show_grid = include_grid and effective_workspace.layout.spec.carto.show_grid

    populate_fn = getattr(styles, "populate_project_layers", None)
    if populate_fn is None:
        populate_fn = getattr(pyqgis_worker, "populate_project_layers", None)

    if populate_fn is not None:
        sig = inspect.signature(populate_fn)
        kwargs: dict[str, Any] = {}
        if "include_grid" in sig.parameters:
            kwargs["include_grid"] = effective_show_grid
        if "project" in sig.parameters and "workspace" in sig.parameters:
            ordered = list(
                populate_fn(project=project, workspace=effective_workspace, **kwargs)
            )
        else:
            ordered = list(populate_fn(project, effective_workspace, **kwargs))
    else:
        ordered = list(pyqgis_worker._assemble_project_layers(project, effective_workspace))

    if not effective_show_grid:
        ordered = [lyr for lyr in ordered if not _is_mgrs_layer(lyr, workspace.mgrs_gpkg)]

    return ordered


def _qimage_to_rgb_bytes(qimg: Any) -> bytes:
    """Extract tightly packed RGB888 bytes from a Qt QImage, respecting scanline alignment."""
    from qgis.PyQt.QtGui import QImage

    rgb_img = qimg.convertToFormat(QImage.Format_RGB888)
    width = rgb_img.width()
    height = rgb_img.height()
    row_bytes = width * 3
    stride = rgb_img.bytesPerLine()

    ptr = rgb_img.constBits()
    ptr.setsize(stride * height)
    raw = bytes(ptr)

    if stride == row_bytes:
        return raw

    # Strip Qt 4-byte scanline alignment padding at the end of each row.
    chunks = [raw[y * stride : y * stride + row_bytes] for y in range(height)]
    return b"".join(chunks)


def _write_qimage_geotiff(
    output_tif: Path,
    qimg: Any,
    *,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    utm_epsg: int,
    strip_rows: int = 512,
) -> Path:
    """Stream RGB888 scanlines from a Qt QImage directly into a UTM GeoTIFF in bounded strips."""
    from osgeo import gdal, osr
    from qgis.PyQt.QtGui import QImage

    gdal.UseExceptions()
    osr.UseExceptions()

    rgb_img = qimg.convertToFormat(QImage.Format_RGB888)
    width_px = rgb_img.width()
    height_px = rgb_img.height()
    stride = rgb_img.bytesPerLine()

    output_tif.parent.mkdir(parents=True, exist_ok=True)
    if output_tif.exists():
        output_tif.unlink()

    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        raise RuntimeError("GDAL GTiff driver is unavailable.")

    ds = driver.Create(
        str(output_tif),
        width_px,
        height_px,
        3,
        gdal.GDT_Byte,
        options=[
            "COMPRESS=NONE",
            "TILED=YES",
            "PHOTOMETRIC=RGB",
            "INTERLEAVE=PIXEL",
        ],
    )
    if ds is None:
        raise RuntimeError(f"Failed to create GeoTIFF at '{output_tif}'.")

    try:
        pixel_w = (max_x - min_x) / width_px
        pixel_h = (max_y - min_y) / height_px
        ds.SetGeoTransform((min_x, pixel_w, 0.0, max_y, 0.0, -pixel_h))

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(utm_epsg)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ds.SetProjection(srs.ExportToWkt())

        for band_idx, interp in (
            (1, gdal.GCI_RedBand),
            (2, gdal.GCI_GreenBand),
            (3, gdal.GCI_BlueBand),
        ):
            ds.GetRasterBand(band_idx).SetColorInterpretation(interp)

        ptr = rgb_img.constBits()
        ptr.setsize(stride * height_px)
        buf_view = memoryview(ptr)
        for y0 in range(0, height_px, strip_rows):
            rows = min(strip_rows, height_px - y0)
            strip_mv = buf_view[y0 * stride : (y0 + rows) * stride]
            ds.WriteRaster(
                0,
                y0,
                width_px,
                rows,
                strip_mv.tobytes(),
                buf_xsize=width_px,
                buf_ysize=rows,
                buf_type=gdal.GDT_Byte,
                band_list=[1, 2, 3],
                buf_pixel_space=3,
                buf_line_space=stride,
                buf_band_space=1,
            )
        ds.FlushCache()
    finally:
        ds = None

    return output_tif


def _write_rgb_geotiff(
    output_tif: Path,
    rgb_bytes: bytes,
    width_px: int,
    height_px: int,
    *,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    utm_epsg: int,
) -> Path:
    """Write tightly packed RGB888 bytes to a compressed UTM-georeferenced GeoTIFF."""
    from osgeo import gdal, osr

    gdal.UseExceptions()
    osr.UseExceptions()

    output_tif.parent.mkdir(parents=True, exist_ok=True)
    if output_tif.exists():
        output_tif.unlink()

    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        raise RuntimeError("GDAL GTiff driver is unavailable.")

    ds = driver.Create(
        str(output_tif),
        width_px,
        height_px,
        3,
        gdal.GDT_Byte,
        options=[
            "COMPRESS=LZW",
            "TILED=YES",
            "PHOTOMETRIC=RGB",
            "INTERLEAVE=PIXEL",
        ],
    )
    if ds is None:
        raise RuntimeError(f"Failed to create GeoTIFF at '{output_tif}'.")

    try:
        pixel_w = (max_x - min_x) / width_px
        pixel_h = (max_y - min_y) / height_px
        ds.SetGeoTransform((min_x, pixel_w, 0.0, max_y, 0.0, -pixel_h))

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(utm_epsg)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ds.SetProjection(srs.ExportToWkt())

        for band_idx, interp in (
            (1, gdal.GCI_RedBand),
            (2, gdal.GCI_GreenBand),
            (3, gdal.GCI_BlueBand),
        ):
            ds.GetRasterBand(band_idx).SetColorInterpretation(interp)

        ds.WriteRaster(
            0,
            0,
            width_px,
            height_px,
            rgb_bytes,
            buf_xsize=width_px,
            buf_ysize=height_px,
            buf_type=gdal.GDT_Byte,
            band_list=[1, 2, 3],
            buf_pixel_space=3,
            buf_line_space=width_px * 3,
            buf_band_space=1,
        )
        ds.FlushCache()
    finally:
        ds = None

    return output_tif


def render_collarless_geotiff_in_process(
    workspace: JobWorkspace,
    output_tif: Path,
    *,
    dpi: int = 300,
    include_grid: bool = True,
) -> Path:
    """Render collarless UTM GeoTIFF inside the current PyQGIS process (subprocess target)."""
    from qgis.core import (
        Qgis,
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsMapRendererSequentialJob,
        QgsMapSettings,
        QgsProject,
        QgsRectangle,
    )
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor

    from gridforge.render.styles import map_background_hex

    layout = workspace.layout
    width_px, height_px = compute_collarless_dimensions(
        layout.frame_width_mm,
        layout.frame_height_mm,
        dpi,
    )

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

    def _execute() -> Path:
        project = QgsProject.instance()
        project.clear()

        ordered_layers = _populate_layers_for_export(
            project,
            workspace,
            include_grid=include_grid,
        )

        settings = QgsMapSettings()
        settings.setDestinationCrs(QgsCoordinateReferenceSystem(f"EPSG:{layout.utm_epsg}"))
        settings.setOutputSize(QSize(width_px, height_px))
        settings.setOutputDpi(float(dpi))
        settings.setBackgroundColor(QColor(map_background_hex(layout.spec.color_mode)))
        settings.setLayers(ordered_layers)

        # Enable antialiasing and PAL labeling flags across QGIS 3.40 enum conventions.
        flag_enum = getattr(Qgis, "MapSettingsFlag", QgsMapSettings)
        for flag_name in ("Antialiasing", "DrawLabeling", "UseRenderingOptimization"):
            flag = getattr(flag_enum, flag_name, getattr(QgsMapSettings, flag_name, None))
            if flag is not None:
                settings.setFlag(flag, True)

        bbox = layout.utm_bbox
        settings.setExtent(QgsRectangle(bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y))

        # Use the exact visibleExtent QGIS renders into [0..width_px, 0..height_px]
        # so georeferencing has zero half-pixel aspect-rounding drift.
        actual_ext = settings.visibleExtent()

        job = QgsMapRendererSequentialJob(settings)
        job.start()
        job.waitForFinished()

        rendered_qimg = job.renderedImage()
        if rendered_qimg.isNull():
            raise RuntimeError("PyQGIS collarless render produced a null QImage.")

        out_path = _write_qimage_geotiff(
            output_tif,
            rendered_qimg,
            min_x=float(actual_ext.xMinimum()),
            min_y=float(actual_ext.yMinimum()),
            max_x=float(actual_ext.xMaximum()),
            max_y=float(actual_ext.yMaximum()),
            utm_epsg=layout.utm_epsg,
        )

        del rendered_qimg
        del job
        del settings
        ordered_layers.clear()
        del ordered_layers
        project.clear()

        return out_path

    try:
        return _execute()
    finally:
        gc.collect()
        qgs.exitQgis()


def render_collarless_geotiff(
    workspace: JobWorkspace,
    output_tif: Path,
    *,
    dpi: int = 300,
    include_grid: bool = True,
    timeout_seconds: int = 300,
    use_xvfb: bool | None = None,
) -> Path:
    """Spawn an isolated PyQGIS subprocess to render a collarless UTM GeoTIFF."""
    caller_output_tif = Path(output_tif)
    resolved_output_tif = caller_output_tif.resolve()
    resolved_output_tif.parent.mkdir(parents=True, exist_ok=True)

    workspace.job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = workspace.job_dir / "export_raster_manifest.json"
    manifest_path.write_text(workspace.model_dump_json(indent=2), encoding="utf-8")

    resolved_xvfb = (
        use_xvfb
        if use_xvfb is not None
        else os.environ.get("GRIDFORGE_USE_XVFB", "").strip().lower()
        in ("1", "true", "yes")
    )

    cmd: list[str] = []
    if resolved_xvfb:
        cmd.extend(["xvfb-run", "-a"])
    cmd.extend(
        [
            sys.executable,
            "-m",
            "gridforge.export.raster",
            str(manifest_path),
            str(resolved_output_tif),
            "--dpi",
            str(dpi),
        ]
    )
    if not include_grid:
        cmd.append("--no-grid")

    from gridforge.subprocess_env import build_safe_subprocess_env

    env = build_safe_subprocess_env(
        extra={"QT_QPA_PLATFORM": "xcb" if resolved_xvfb else os.environ.get("QT_QPA_PLATFORM", "offscreen")},
        include_linz_key=False,
        allow_network=False,
        private_runtime_dir=workspace.job_dir / ".qt_runtime",
    )

    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"PyQGIS collarless raster worker failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-3000:]}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}"
        )

    if not resolved_output_tif.is_file():
        raise RuntimeError(
            f"Collarless raster worker exited 0 but '{resolved_output_tif}' was not created."
        )

    return caller_output_tif


def _worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GridForge isolated PyQGIS collarless GeoTIFF worker"
    )
    parser.add_argument("manifest_json", type=Path, help="Serialized JobWorkspace JSON")
    parser.add_argument("output_tif", type=Path, help="Target GeoTIFF output path")
    parser.add_argument("--dpi", type=int, default=300, help="Render resolution in DPI")
    parser.add_argument(
        "--no-grid",
        action="store_true",
        help="Hide MGRS grid layers on exported map",
    )
    args = parser.parse_args(argv)

    workspace = JobWorkspace.model_validate_json(
        args.manifest_json.read_text(encoding="utf-8")
    )
    out = render_collarless_geotiff_in_process(
        workspace,
        args.output_tif,
        dpi=args.dpi,
        include_grid=not args.no_grid,
    )
    print(json.dumps({"output_tif": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
