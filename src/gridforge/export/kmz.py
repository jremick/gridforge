"""Garmin Custom Maps (.kmz) exporter respecting hardware tile & baseline JPEG limits."""

from __future__ import annotations

import io
import math
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from osgeo import gdal
from PIL import Image

from gridforge.config import (
    GARMIN_PROFILE_MAX_TILES,
    ColorMode,
    ExportFormat,
    ExportResult,
    GarminProfile,
    JobWorkspace,
)
from gridforge.export.raster import render_collarless_geotiff


def _run_cmd(cmd: list[str], *, step: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-2500:]}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}"
        )


def _paper_bg_rgb(color_mode: ColorMode) -> tuple[int, int, int]:
    return (250, 249, 245) if color_mode == ColorMode.COLOR else (255, 255, 255)


def compute_optimal_tile_grid(
    width_px: int,
    height_px: int,
    *,
    max_tile_px: int,
    tile_cap: int,
) -> tuple[int, int, int, int]:
    """Choose (target_w, target_h, cols, rows) maximizing resolution within tile_cap & max_tile_px."""
    if width_px <= 0 or height_px <= 0:
        raise ValueError(f"Raster dimensions must be positive, got {width_px} x {height_px}.")
    if max_tile_px <= 0:
        raise ValueError(f"max_tile_px must be positive, got {max_tile_px}.")
    if tile_cap <= 0:
        raise ValueError(f"tile_cap must be positive, got {tile_cap}.")

    orig_cols = math.ceil(width_px / max_tile_px)
    orig_rows = math.ceil(height_px / max_tile_px)
    if orig_cols * orig_rows <= tile_cap:
        return (width_px, height_px, orig_cols, orig_rows)

    best_scale = -1.0
    best_grid = (1, 1)

    max_r = min(tile_cap, orig_rows)
    for r in range(1, max_r + 1):
        c = min(orig_cols, tile_cap // r)
        if c < 1:
            continue
        scale = min(1.0, (c * max_tile_px) / width_px, (r * max_tile_px) / height_px)
        if scale > best_scale:
            best_scale = scale
            best_grid = (c, r)

    cols, rows = best_grid
    target_w = min(cols * max_tile_px, max(1, int(math.floor(width_px * best_scale))))
    target_h = min(rows * max_tile_px, max(1, int(math.floor(height_px * best_scale))))

    # Recompute exact tight tile grid in case rounding saved a row or column.
    final_cols = math.ceil(target_w / max_tile_px)
    final_rows = math.ceil(target_h / max_tile_px)
    return (target_w, target_h, final_cols, final_rows)


def is_baseline_jpeg(data: bytes) -> bool:
    """Scan JPEG markers to confirm baseline DCT (SOF0 0xFFC0) and reject progressive (SOF2 0xFFC2)."""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return False

    has_sof0 = False
    pos = 2
    n = len(data)
    while pos + 1 < n:
        if data[pos] != 0xFF:
            return False
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1

        # Standalone markers with no payload length
        if marker in (0x01, 0xD8, 0xD9) or (0xD0 <= marker <= 0xD7):
            if marker == 0xD9:
                break
            continue
        # Start of Scan (entropy-coded payload follows until EOI)
        if marker == 0xDA:
            break

        if marker == 0xC0:
            has_sof0 = True
        elif marker == 0xC2:
            return False

        if pos + 2 > n:
            break
        seg_len = (data[pos] << 8) | data[pos + 1]
        if seg_len < 2:
            return False
        pos += seg_len

    return has_sof0


def _encode_baseline_jpeg(
    tile_img: Image.Image,
    *,
    jpeg_quality: int,
    max_tile_bytes: int,
) -> bytes:
    """Encode a PIL Image strictly as baseline non-progressive RGB JPEG <= max_tile_bytes."""
    rgb = tile_img.convert("RGB")
    ladder = [jpeg_quality, 75, 65, 55, 45, 35, 25, 15, 10]
    qualities: list[int] = []
    for q in ladder:
        if q <= jpeg_quality and q not in qualities:
            qualities.append(q)
    if not qualities:
        qualities = [jpeg_quality]

    last_len = 0
    for q in qualities:
        buf = io.BytesIO()
        rgb.save(
            buf,
            format="JPEG",
            quality=q,
            progressive=False,
            optimize=True,
            subsampling="4:2:0",
        )
        data = buf.getvalue()
        last_len = len(data)
        if last_len <= max_tile_bytes:
            if not is_baseline_jpeg(data):
                raise RuntimeError("Encoded JPEG failed baseline SOF0 verification.")
            return data

    raise RuntimeError(
        f"Unable to compress Garmin KMZ tile ({rgb.width}x{rgb.height}) below "
        f"max_tile_bytes={max_tile_bytes} (smallest encoded size={last_len} bytes)."
    )


def _read_tile_rgb(
    ds: gdal.Dataset,
    x0: int,
    y0: int,
    tw: int,
    th: int,
    bg_rgb: tuple[int, int, int],
) -> Image.Image:
    """Read an (x0, y0, tw, th) pixel window from a GDAL dataset as an RGB PIL Image."""
    band_count = ds.RasterCount
    if band_count >= 4:
        raw_rgba = ds.ReadRaster(
            x0,
            y0,
            tw,
            th,
            buf_xsize=tw,
            buf_ysize=th,
            buf_type=gdal.GDT_Byte,
            band_list=[1, 2, 3, 4],
            buf_pixel_space=4,
            buf_line_space=tw * 4,
            buf_band_space=1,
        )
        rgba = Image.frombytes("RGBA", (tw, th), raw_rgba)
        canvas = Image.new("RGBA", (tw, th), (*bg_rgb, 255))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")

    if band_count == 1:
        raw_gray = ds.ReadRaster(
            x0,
            y0,
            tw,
            th,
            buf_xsize=tw,
            buf_ysize=th,
            buf_type=gdal.GDT_Byte,
            band_list=[1],
        )
        return Image.frombytes("L", (tw, th), raw_gray).convert("RGB")

    raw_rgb = ds.ReadRaster(
        x0,
        y0,
        tw,
        th,
        buf_xsize=tw,
        buf_ysize=th,
        buf_type=gdal.GDT_Byte,
        band_list=[1, 2, 3],
        buf_pixel_space=3,
        buf_line_space=tw * 3,
        buf_band_space=1,
    )
    return Image.frombytes("RGB", (tw, th), raw_rgb)


def _build_kml(
    folder_name: str,
    description: str,
    draw_order: int,
    overlays: list[tuple[str, str, float, float, float, float]],
) -> str:
    """Construct valid KML 2.2 doc.kml with upright WGS84 GroundOverlays (zero rotation)."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "  <Folder>",
        f"    <name>{escape(folder_name)}</name>",
        f"    <description>{escape(description)}</description>",
    ]
    for tile_name, href, north, south, east, west in overlays:
        lines.extend(
            [
                "    <GroundOverlay>",
                f"      <name>{escape(tile_name)}</name>",
                f"      <drawOrder>{draw_order}</drawOrder>",
                "      <Icon>",
                f"        <href>{escape(href)}</href>",
                "      </Icon>",
                "      <LatLonBox>",
                f"        <north>{north:.8f}</north>",
                f"        <south>{south:.8f}</south>",
                f"        <east>{east:.8f}</east>",
                f"        <west>{west:.8f}</west>",
                "      </LatLonBox>",
                "    </GroundOverlay>",
            ]
        )
    lines.extend(["  </Folder>", "</kml>", ""])
    return "\n".join(lines)


def export_garmin_kmz(
    workspace: JobWorkspace,
    output_kmz: Path,
    *,
    profile: GarminProfile = GarminProfile.STANDARD,
    max_tiles: int | None = None,
    max_tile_px: int = 1024,
    max_tile_bytes: int = 3_000_000,
    jpeg_quality: int = 85,
    draw_order: int = 60,
    dpi: int = 300,
    include_grid: bool = True,
    collarless_tif: Path | None = None,
) -> ExportResult:
    """Export the exact sheet map body as a Garmin Custom Maps (.kmz) archive."""
    t0 = time.perf_counter()

    tile_cap = (
        max_tiles if max_tiles is not None else GARMIN_PROFILE_MAX_TILES[profile]
    )
    if tile_cap < 1:
        raise ValueError(f"max_tiles must be >= 1, got {tile_cap}.")
    if max_tile_px < 16 or max_tile_px > 1024:
        raise ValueError(
            f"max_tile_px must be within 16..1024 for Garmin compatibility, got {max_tile_px}."
        )
    if max_tile_bytes < 1024:
        raise ValueError(f"max_tile_bytes must be >= 1024, got {max_tile_bytes}.")
    if not 1 <= jpeg_quality <= 100:
        raise ValueError(f"jpeg_quality must be in 1..100, got {jpeg_quality}.")
    if not 50 <= draw_order <= 75:
        raise ValueError(
            f"draw_order must be within 50..75 for Garmin Custom Maps, got {draw_order}."
        )

    caller_out = Path(output_kmz)
    resolved_out = caller_out.resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    if resolved_out.exists():
        resolved_out.unlink()

    layout = workspace.layout
    spec = layout.spec
    bg_rgb = _paper_bg_rgb(spec.color_mode)

    gdal.UseExceptions()

    with tempfile.TemporaryDirectory(prefix="gridforge_kmz_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)

        if collarless_tif is not None and Path(collarless_tif).is_file():
            src_tif = Path(collarless_tif).resolve()
        else:
            src_tif = render_collarless_geotiff(
                workspace,
                tmp_dir / "collarless_utm.tif",
                dpi=dpi,
                include_grid=include_grid,
            ).resolve()

        # Warp to upright north-up WGS84 (EPSG:4326) with matching paper background fill.
        warped_4326 = tmp_dir / "warped_4326.tif"
        ds_src = gdal.Open(str(src_tif))
        src_bands = ds_src.RasterCount if ds_src is not None else 3
        ds_src = None

        init_dest = (
            f"{bg_rgb[0]},{bg_rgb[1]},{bg_rgb[2]},255"
            if src_bands >= 4
            else f"{bg_rgb[0]},{bg_rgb[1]},{bg_rgb[2]}"
        )
        _run_cmd(
            [
                "gdalwarp",
                "-overwrite",
                "-t_srs",
                "EPSG:4326",
                "-r",
                "lanczos",
                "-wo",
                f"INIT_DEST={init_dest}",
                "-co",
                "COMPRESS=LZW",
                str(src_tif),
                str(warped_4326),
            ],
            step="gdalwarp EPSG:4326 for Garmin KMZ",
        )

        ds = gdal.Open(str(warped_4326))
        if ds is None:
            raise RuntimeError(f"Failed to open warped EPSG:4326 raster '{warped_4326}'.")
        try:
            orig_w = ds.RasterXSize
            orig_h = ds.RasterYSize
            gt = ds.GetGeoTransform()
        finally:
            ds = None

        if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12:
            raise RuntimeError(
                f"Warped EPSG:4326 raster has non-zero rotation terms in GeoTransform: {gt}"
            )

        west = float(gt[0])
        east = float(gt[0] + orig_w * gt[1])
        north = float(gt[3])
        south = float(gt[3] + orig_h * gt[5])

        target_w, target_h, cols, rows = compute_optimal_tile_grid(
            orig_w,
            orig_h,
            max_tile_px=max_tile_px,
            tile_cap=tile_cap,
        )

        tile_source_tif = warped_4326
        if (target_w, target_h) != (orig_w, orig_h):
            resized_tif = tmp_dir / "resized_4326.tif"
            _run_cmd(
                [
                    "gdal_translate",
                    "-outsize",
                    str(target_w),
                    str(target_h),
                    "-r",
                    "lanczos",
                    "-co",
                    "COMPRESS=LZW",
                    str(warped_4326),
                    str(resized_tif),
                ],
                step="gdal_translate Lanczos auto-downscale for Garmin KMZ",
            )
            tile_source_tif = resized_tif

        # Balanced contiguous partition guaranteeing every tile <= max_tile_px and zero gaps.
        x_bounds = [(i * target_w) // cols for i in range(cols + 1)]
        y_bounds = [(j * target_h) // rows for j in range(rows + 1)]

        overlays: list[tuple[str, str, float, float, float, float]] = []
        max_w_seen = 0
        max_h_seen = 0
        max_bytes_seen = 0

        folder_name = spec.title or f"GridForge {layout.gzd} {workspace.mgrs_meta.center_mgrs_10m}"
        attributions = (
            " | ".join(workspace.attributions)
            if workspace.attributions
            else "Map data © OpenStreetMap contributors (ODbL)"
        )
        description = (
            f"GridForge 1:{spec.scale:,} {spec.color_mode.value.upper()} Garmin Custom Map "
            f"({layout.gzd}, EPSG:{layout.utm_epsg}, {workspace.vertical_datum}). {attributions}"
        )

        ds = gdal.Open(str(tile_source_tif))
        if ds is None:
            raise RuntimeError(f"Failed to open KMZ tile source '{tile_source_tif}'.")

        try:
            with zipfile.ZipFile(resolved_out, mode="w") as zf:
                for r in range(rows):
                    y0, y1 = y_bounds[r], y_bounds[r + 1]
                    th = y1 - y0
                    tile_north = north - (y0 / target_h) * (north - south)
                    tile_south = north - (y1 / target_h) * (north - south)

                    for c in range(cols):
                        x0, x1 = x_bounds[c], x_bounds[c + 1]
                        tw = x1 - x0
                        tile_west = west + (x0 / target_w) * (east - west)
                        tile_east = west + (x1 / target_w) * (east - west)

                        tile_img = _read_tile_rgb(ds, x0, y0, tw, th, bg_rgb)
                        jpg_bytes = _encode_baseline_jpeg(
                            tile_img,
                            jpeg_quality=jpeg_quality,
                            max_tile_bytes=max_tile_bytes,
                        )

                        href = f"files/tile_r{r:02d}_c{c:02d}.jpg"
                        tile_name = f"Tile r{r:02d} c{c:02d}"
                        zf.writestr(href, jpg_bytes, compress_type=zipfile.ZIP_STORED)

                        overlays.append(
                            (tile_name, href, tile_north, tile_south, tile_east, tile_west)
                        )
                        max_w_seen = max(max_w_seen, tw)
                        max_h_seen = max(max_h_seen, th)
                        max_bytes_seen = max(max_bytes_seen, len(jpg_bytes))

                kml_text = _build_kml(folder_name, description, draw_order, overlays)
                zf.writestr(
                    "doc.kml",
                    kml_text.encode("utf-8"),
                    compress_type=zipfile.ZIP_DEFLATED,
                )
        finally:
            ds = None

    elapsed = round(time.perf_counter() - t0, 3)
    return ExportResult(
        format=ExportFormat.KMZ,
        output_path=caller_out,
        file_size_bytes=resolved_out.stat().st_size,
        tile_count=len(overlays),
        min_zoom=None,
        max_zoom=None,
        max_tile_px=(max_w_seen, max_h_seen),
        max_tile_bytes=max_bytes_seen,
        elapsed_seconds=elapsed,
    )
