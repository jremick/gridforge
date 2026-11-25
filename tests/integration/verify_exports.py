"""Automated verification library & CLI for GridForge Phase 3 offline device exports.

Validates:
  - ATAK MBTiles 1.3 archives (`.mbtiles`): SQLite schema, required metadata keys,
    multi-zoom overview pyramid, valid TMS tile coordinates, PNG/JPEG magic bytes,
    256x256 Pillow decode, non-blank tile content, and stitched TMS mosaic extraction.
  - Garmin Custom Maps archives (`.kmz`): ZIP integrity, valid KML 2.2 `<GroundOverlay>`
    structure, tile-count caps (<= 100 standard / <= 500 modern / custom cap),
    per-tile <= 1024x1024 px and <= 3 MB caps, strict baseline JPEG (SOF0 0xFFC0 present,
    NO progressive SOF2 0xFFC2), valid WGS84 `<LatLonBox>` tiling, `<drawOrder>` 50-75,
    and sample tile / stitched overview extraction for visual inspection.

CLI usage:
    python tests/integration/verify_exports.py out.mbtiles --format png --extract-dir /tmp/inspect
    python tests/integration/verify_exports.py out.kmz --max-tiles 100 --extract-dir /tmp/inspect
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_SOI = b"\xff\xd8"

REQUIRED_MBTILES_METADATA: tuple[str, ...] = (
    "name",
    "format",
    "bounds",
    "center",
    "minzoom",
    "maxzoom",
    "type",
    "description",
    "attribution",
)

KML_NS = "http://www.opengis.net/kml/2.2"


def _scan_jpeg_sof_markers(data: bytes) -> tuple[bool, bool, list[int]]:
    """Structurally parse JPEG marker segments before SOS (0xDA) to detect SOF frame types.

    Returns ``(has_sof0_baseline, has_sof2_progressive, all_sof_markers)``.
    Avoids false positives from raw bytes inside entropy-coded scan data or metadata blocks.
    """
    if len(data) < 4 or data[:2] != JPEG_SOI:
        return (False, False, [])

    pos = 2
    n = len(data)
    sof_markers: list[int] = []

    while pos < n:
        # Skip fill bytes (0xFF padding)
        if data[pos] != 0xFF:
            break
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break

        marker = data[pos]
        pos += 1

        # End of Image or Start of Scan (entropy data follows; SOF always precedes SOS)
        if marker in (0xD9, 0xDA):
            break
        # Standalone markers without length field (TEM 0x01, RST0..RST7 0xD0..0xD7, SOI 0xD8)
        if marker == 0x01 or (0xD0 <= marker <= 0xD8):
            continue

        # Record Start-Of-Frame markers (0xC0..0xCF excluding DHT=0xC4, JPG=0xC8, DAC=0xCC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            sof_markers.append(marker)

        if pos + 2 > n:
            break
        seg_len = (data[pos] << 8) | data[pos + 1]
        if seg_len < 2:
            break
        pos += seg_len

    has_sof0 = 0xC0 in sof_markers
    has_sof2 = 0xC2 in sof_markers
    return (has_sof0, has_sof2, sof_markers)


def _image_has_non_constant_pixels(img: Image.Image) -> bool:
    """Return True if at least one RGB channel has variation (not a blank solid tile)."""
    rgb = img.convert("RGB")
    extrema = rgb.getextrema()
    return any(lo != hi for lo, hi in extrema)


@dataclass
class MBTilesVerificationReport:
    """Structured result of automated ATAK MBTiles 1.3 verification."""

    mbtiles_path: Path
    file_size_bytes: int
    metadata: dict[str, str]
    tile_count: int
    zoom_levels: list[int]
    tiles_per_zoom: dict[int, int]
    sampled_tiles_checked: int
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mbtiles_path": str(self.mbtiles_path),
            "passed": self.passed,
            "file_size_bytes": self.file_size_bytes,
            "metadata": self.metadata,
            "tile_count": self.tile_count,
            "zoom_levels": self.zoom_levels,
            "tiles_per_zoom": self.tiles_per_zoom,
            "sampled_tiles_checked": self.sampled_tiles_checked,
            "checks": self.checks,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"[{status}] MBTiles Verification: {self.mbtiles_path}",
            f"  File size   : {self.file_size_bytes:,} bytes",
            f"  Format      : {self.metadata.get('format', '<missing>')}",
            f"  Zoom levels : {self.zoom_levels} (total tiles={self.tile_count:,}, per_zoom={self.tiles_per_zoom})",
            f"  Bounds      : {self.metadata.get('bounds', '<missing>')}",
            f"  Center      : {self.metadata.get('center', '<missing>')}",
            f"  Sampled     : {self.sampled_tiles_checked} tiles decoded & verified",
        ]
        if self.warnings:
            lines.append("  Warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")
        if self.errors:
            lines.append("  ERRORS:")
            for e in self.errors:
                lines.append(f"    - {e}")
        return "\n".join(lines)


def verify_mbtiles(
    path: Path | str,
    *,
    expected_format: str = "png",
    expected_bounds: tuple[float, float, float, float] | None = None,
    require_multiple_zooms: bool = True,
    max_sample_tiles: int = 16,
) -> MBTilesVerificationReport:
    """Validate an ATAK-compatible MBTiles 1.3 SQLite archive."""
    mbt_path = Path(path)
    if not mbt_path.is_file():
        raise FileNotFoundError(f"MBTiles file does not exist: {mbt_path}")

    exp_fmt = expected_format.strip().lower()
    if exp_fmt == "jpeg":
        exp_fmt = "jpg"
    if exp_fmt not in ("png", "jpg"):
        raise ValueError(f"Unsupported expected_format={expected_format!r}; use 'png' or 'jpg'.")

    file_size = mbt_path.stat().st_size
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}
    metadata: dict[str, str] = {}
    tile_count = 0
    zoom_levels: list[int] = []
    tiles_per_zoom: dict[int, int] = {}
    sampled_checked = 0

    checks["non_empty_file"] = file_size > 0
    if file_size <= 0:
        errors.append("MBTiles archive is 0 bytes.")

    conn = sqlite3.connect(f"file:{mbt_path}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        has_schema = {"metadata", "tiles"}.issubset(tables)
        checks["schema_tables"] = has_schema
        if not has_schema:
            errors.append(f"Missing required MBTiles tables ('metadata', 'tiles'); found {sorted(tables)}.")
            return MBTilesVerificationReport(
                mbtiles_path=mbt_path,
                file_size_bytes=file_size,
                metadata=metadata,
                tile_count=0,
                zoom_levels=[],
                tiles_per_zoom={},
                sampled_tiles_checked=0,
                checks=checks,
                errors=errors,
                warnings=warnings,
            )

        for name, val in conn.execute("SELECT name, value FROM metadata").fetchall():
            if name is not None:
                metadata[str(name)] = "" if val is None else str(val)

        # 1. Required MBTiles 1.3 metadata keys
        missing_keys = [k for k in REQUIRED_MBTILES_METADATA if not metadata.get(k, "").strip()]
        checks["required_metadata"] = len(missing_keys) == 0
        if missing_keys:
            errors.append(f"Missing or empty required MBTiles 1.3 metadata key(s): {missing_keys}.")

        # Format check
        actual_fmt = metadata.get("format", "").strip().lower()
        fmt_match = (actual_fmt == "png" and exp_fmt == "png") or (
            actual_fmt in ("jpg", "jpeg") and exp_fmt == "jpg"
        )
        checks["metadata_format"] = fmt_match
        if not fmt_match:
            errors.append(
                f"MBTiles metadata format={actual_fmt!r} does not match expected {exp_fmt!r}."
            )

        # Bounds check
        parsed_bounds: tuple[float, float, float, float] | None = None
        if "bounds" in metadata:
            try:
                b_parts = [float(x.strip()) for x in metadata["bounds"].split(",")]
                if len(b_parts) != 4:
                    raise ValueError("expected 4 comma-separated floats")
                w, s, e, n = b_parts
                if not (-180.0 <= w < e <= 180.0 and -85.06 <= s < n <= 85.06):
                    raise ValueError(f"invalid WGS84 bounds {(w, s, e, n)}")
                parsed_bounds = (w, s, e, n)
                checks["metadata_bounds"] = True
            except Exception as exc:
                checks["metadata_bounds"] = False
                errors.append(f"Invalid MBTiles 'bounds' metadata {metadata.get('bounds')!r}: {exc}")

        if expected_bounds is not None and parsed_bounds is not None:
            ew, es, ee, en = expected_bounds
            bw, bs, be, bn = parsed_bounds
            # MBTiles tile-aligned bounds should enclose or closely match expected WGS84 footprint
            tol = 0.05
            bounds_ok = bw <= ew + tol and be >= ee - tol and bs <= es + tol and bn >= en - tol
            checks["expected_bounds_match"] = bounds_ok
            if not bounds_ok:
                errors.append(
                    f"MBTiles bounds {parsed_bounds} do not cover expected WGS84 bounds {expected_bounds}."
                )

        # Center check
        if "center" in metadata:
            try:
                c_parts = [float(x.strip()) for x in metadata["center"].split(",")]
                if len(c_parts) != 3:
                    raise ValueError("expected lon,lat,zoom")
                clon, clat, cz = c_parts
                if not (-180.0 <= clon <= 180.0 and -85.06 <= clat <= 85.06 and 0 <= cz <= 24):
                    raise ValueError(f"out of range {c_parts}")
                checks["metadata_center"] = True
            except Exception as exc:
                checks["metadata_center"] = False
                errors.append(f"Invalid MBTiles 'center' metadata {metadata.get('center')!r}: {exc}")

        # 2. Tile inventory, zoom pyramid, and TMS coordinate bounds
        rows = conn.execute(
            """
            SELECT zoom_level, COUNT(*), MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row)
            FROM tiles
            GROUP BY zoom_level
            ORDER BY zoom_level ASC
            """
        ).fetchall()

        tms_ok = True
        for z, cnt, min_c, max_c, min_r, max_r in rows:
            z_int = int(z)
            zoom_levels.append(z_int)
            tiles_per_zoom[z_int] = int(cnt)
            tile_count += int(cnt)
            limit = 1 << z_int
            if not (0 <= min_c <= max_c < limit and 0 <= min_r <= max_r < limit):
                tms_ok = False
                errors.append(
                    f"Zoom {z_int} has out-of-range TMS coordinates: "
                    f"col=[{min_c}, {max_c}], row=[{min_r}, {max_r}], limit={limit}."
                )

        checks["tiles_non_empty"] = tile_count > 0
        if tile_count == 0:
            errors.append("MBTiles 'tiles' table contains 0 tiles.")

        checks["tms_row_range"] = tms_ok

        multi_zoom_ok = (not require_multiple_zooms) or (len(zoom_levels) >= 2)
        checks["multiple_zoom_pyramid"] = multi_zoom_ok
        if not multi_zoom_ok:
            errors.append(
                f"Expected multi-zoom overview pyramid (>= 2 zoom levels), found only {zoom_levels}."
            )

        if zoom_levels and "minzoom" in metadata and "maxzoom" in metadata:
            try:
                meta_min_z = int(metadata["minzoom"])
                meta_max_z = int(metadata["maxzoom"])
                z_consistent = meta_min_z == zoom_levels[0] and meta_max_z == zoom_levels[-1]
                checks["zoom_metadata_consistent"] = z_consistent
                if not z_consistent:
                    errors.append(
                        f"Metadata minzoom/maxzoom ({meta_min_z}..{meta_max_z}) "
                        f"mismatch actual tile zooms ({zoom_levels[0]}..{zoom_levels[-1]})."
                    )
            except ValueError as exc:
                checks["zoom_metadata_consistent"] = False
                errors.append(f"Non-integer minzoom/maxzoom metadata: {exc}")

        # 3. Sample tile magic bytes + Pillow decode + non-blank content check
        if tile_count > 0:
            sample_rows: list[tuple[int, int, int, bytes]] = []
            # Pick tiles from max zoom (highest detail) and min zoom (overview)
            target_zooms = [zoom_levels[-1]]
            if len(zoom_levels) > 1:
                target_zooms.append(zoom_levels[0])
            per_z_limit = max(2, max_sample_tiles // len(target_zooms))
            for tz in target_zooms:
                z_tiles = conn.execute(
                    """
                    SELECT zoom_level, tile_column, tile_row, tile_data
                    FROM tiles
                    WHERE zoom_level = ?
                    ORDER BY tile_column, tile_row
                    """,
                    (tz,),
                ).fetchall()
                if len(z_tiles) <= per_z_limit:
                    sample_rows.extend(z_tiles)
                else:
                    step = max(1, len(z_tiles) // per_z_limit)
                    sample_rows.extend(z_tiles[::step][:per_z_limit])

            magic_ok = True
            decode_ok = True
            non_blank_found = False

            for z, col, row, blob in sample_rows:
                raw = bytes(blob)
                sampled_checked += 1
                if exp_fmt == "png":
                    if not raw.startswith(PNG_MAGIC):
                        magic_ok = False
                        errors.append(f"Tile z={z}/{col}/{row} does not start with PNG magic bytes.")
                        continue
                else:
                    if not raw.startswith(b"\xff\xd8\xff"):
                        magic_ok = False
                        errors.append(f"Tile z={z}/{col}/{row} does not start with JPEG magic bytes.")
                        continue

                try:
                    with Image.open(io.BytesIO(raw)) as img:
                        img.load()
                        expected_pil_fmt = "PNG" if exp_fmt == "png" else "JPEG"
                        if img.format != expected_pil_fmt:
                            decode_ok = False
                            errors.append(
                                f"Tile z={z}/{col}/{row} Pillow format={img.format!r}, expected {expected_pil_fmt!r}."
                            )
                        if img.size != (256, 256):
                            decode_ok = False
                            errors.append(
                                f"Tile z={z}/{col}/{row} dimensions={img.size}, expected (256, 256)."
                            )
                        if _image_has_non_constant_pixels(img):
                            non_blank_found = True
                except Exception as exc:
                    decode_ok = False
                    errors.append(f"Failed to decode tile z={z}/{col}/{row} via Pillow: {exc}")

            checks["tile_magic_bytes"] = magic_ok
            checks["tile_pillow_decode"] = decode_ok
            checks["tile_non_blank"] = non_blank_found
            if not non_blank_found:
                errors.append("All sampled MBTiles tiles appear completely blank/single-color.")
    finally:
        conn.close()

    return MBTilesVerificationReport(
        mbtiles_path=mbt_path,
        file_size_bytes=file_size,
        metadata=metadata,
        tile_count=tile_count,
        zoom_levels=zoom_levels,
        tiles_per_zoom=tiles_per_zoom,
        sampled_tiles_checked=sampled_checked,
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def assert_mbtiles_valid(path: Path | str, **kwargs: Any) -> MBTilesVerificationReport:
    """Run verify_mbtiles and raise AssertionError with full diagnostics on failure."""
    report = verify_mbtiles(path, **kwargs)
    if not report.passed:
        raise AssertionError(report.summary())
    return report


def extract_mbtiles_preview(
    path: Path | str,
    out_dir: Path | str,
    *,
    prefix: str | None = None,
    max_grid: int = 4,
) -> dict[str, Path]:
    """Extract a center max-zoom tile and a north-up TMS stitched mosaic PNG for visual inspection."""
    src = Path(path)
    dst = Path(out_dir)
    dst.mkdir(parents=True, exist_ok=True)
    stem = prefix or src.stem

    outputs: dict[str, Path] = {}
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        max_z_row = conn.execute("SELECT MAX(zoom_level) FROM tiles").fetchone()
        if not max_z_row or max_z_row[0] is None:
            raise RuntimeError(f"No tiles in MBTiles archive: {src}")
        max_z = int(max_z_row[0])

        coords = conn.execute(
            "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?",
            (max_z,),
        ).fetchall()
        cols = sorted({int(c) for c, _ in coords})
        rows = sorted({int(r) for _, r in coords})

        # Select center window up to max_grid x max_grid
        c_mid = len(cols) // 2
        r_mid = len(rows) // 2
        c_start = max(0, c_mid - max_grid // 2)
        r_start = max(0, r_mid - max_grid // 2)
        sel_cols = cols[c_start : c_start + max_grid]
        # TMS tile_row increases south->north, so north-up mosaic places highest row at y=0
        sel_rows_north_to_south = list(reversed(rows[r_start : r_start + max_grid]))

        mosaic = Image.new("RGBA", (len(sel_cols) * 256, len(sel_rows_north_to_south) * 256), (255, 255, 255, 255))
        center_tile_saved = False
        center_c = sel_cols[len(sel_cols) // 2]
        center_r = sel_rows_north_to_south[len(sel_rows_north_to_south) // 2]

        for yi, r in enumerate(sel_rows_north_to_south):
            for xi, c in enumerate(sel_cols):
                row_data = conn.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (max_z, c, r),
                ).fetchone()
                if row_data is None:
                    continue
                with Image.open(io.BytesIO(bytes(row_data[0]))) as tile_img:
                    rgba = tile_img.convert("RGBA")
                    mosaic.paste(rgba, (xi * 256, yi * 256))
                    if c == center_c and r == center_r and not center_tile_saved:
                        tile_out = dst / f"{stem}_z{max_z}_center_tile.png"
                        rgba.save(tile_out, format="PNG")
                        outputs["center_tile"] = tile_out
                        center_tile_saved = True

        mosaic_out = dst / f"{stem}_z{max_z}_mosaic.png"
        mosaic.save(mosaic_out, format="PNG")
        outputs["mosaic"] = mosaic_out
    finally:
        conn.close()

    return outputs


@dataclass
class KMZVerificationReport:
    """Structured result of automated Garmin Custom Maps `.kmz` verification."""

    kmz_path: Path
    file_size_bytes: int
    tile_count: int
    max_tiles_allowed: int
    max_observed_px: tuple[int, int]
    max_observed_tile_bytes: int
    union_bounds: tuple[float, float, float, float] | None
    draw_orders: list[int]
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kmz_path": str(self.kmz_path),
            "passed": self.passed,
            "file_size_bytes": self.file_size_bytes,
            "tile_count": self.tile_count,
            "max_tiles_allowed": self.max_tiles_allowed,
            "max_observed_px": list(self.max_observed_px),
            "max_observed_tile_bytes": self.max_observed_tile_bytes,
            "union_bounds": list(self.union_bounds) if self.union_bounds else None,
            "draw_orders": sorted(set(self.draw_orders)),
            "checks": self.checks,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"[{status}] Garmin KMZ Verification: {self.kmz_path}",
            f"  File size       : {self.file_size_bytes:,} bytes",
            f"  Tile count      : {self.tile_count} (cap <= {self.max_tiles_allowed})",
            f"  Max tile px     : {self.max_observed_px[0]} x {self.max_observed_px[1]} px",
            f"  Max tile bytes  : {self.max_observed_tile_bytes:,} bytes",
            f"  Union LatLonBox : {self.union_bounds}",
            f"  Draw orders     : {sorted(set(self.draw_orders))}",
        ]
        if self.warnings:
            lines.append("  Warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")
        if self.errors:
            lines.append("  ERRORS:")
            for e in self.errors:
                lines.append(f"    - {e}")
        return "\n".join(lines)


def _find_kml_child_text(elem: ET.Element, tag_name: str) -> str | None:
    """Find direct or nested child tag matching ``tag_name`` ignoring XML namespace prefix."""
    for child in elem.iter():
        local = child.tag.rsplit("}", 1)[-1]
        if local == tag_name and child.text is not None:
            return child.text.strip()
    return None


def verify_garmin_kmz(
    path: Path | str,
    *,
    max_tiles: int = 100,
    max_px: int = 1024,
    max_tile_bytes: int = 3_000_000,
    expected_bounds: tuple[float, float, float, float] | None = None,
) -> KMZVerificationReport:
    """Validate a Garmin Custom Maps `.kmz` archive against all hardware and KML 2.2 constraints."""
    kmz_path = Path(path)
    if not kmz_path.is_file():
        raise FileNotFoundError(f"KMZ file does not exist: {kmz_path}")

    file_size = kmz_path.stat().st_size
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}

    if not zipfile.is_zipfile(kmz_path):
        errors.append("File is not a valid ZIP archive.")
        return KMZVerificationReport(
            kmz_path=kmz_path,
            file_size_bytes=file_size,
            tile_count=0,
            max_tiles_allowed=max_tiles,
            max_observed_px=(0, 0),
            max_observed_tile_bytes=0,
            union_bounds=None,
            draw_orders=[],
            checks={"valid_zip": False},
            errors=errors,
            warnings=warnings,
        )

    checks["valid_zip"] = True

    max_w = 0
    max_h = 0
    max_bytes = 0
    draw_orders: list[int] = []
    min_west = float("inf")
    min_south = float("inf")
    max_east = float("-inf")
    max_north = float("-inf")
    tile_count = 0

    with zipfile.ZipFile(kmz_path, "r") as zf:
        names = set(zf.namelist())
        has_doc_kml = "doc.kml" in names
        checks["has_doc_kml"] = has_doc_kml
        if not has_doc_kml:
            errors.append(f"KMZ archive missing required 'doc.kml' entry (entries={sorted(names)[:10]}).")
            return KMZVerificationReport(
                kmz_path=kmz_path,
                file_size_bytes=file_size,
                tile_count=0,
                max_tiles_allowed=max_tiles,
                max_observed_px=(0, 0),
                max_observed_tile_bytes=0,
                union_bounds=None,
                draw_orders=[],
                checks=checks,
                errors=errors,
                warnings=warnings,
            )

        try:
            kml_bytes = zf.read("doc.kml")
            root = ET.fromstring(kml_bytes)
        except ET.ParseError as exc:
            checks["valid_kml_xml"] = False
            errors.append(f"doc.kml is not valid XML: {exc}")
            return KMZVerificationReport(
                kmz_path=kmz_path,
                file_size_bytes=file_size,
                tile_count=0,
                max_tiles_allowed=max_tiles,
                max_observed_px=(0, 0),
                max_observed_tile_bytes=0,
                union_bounds=None,
                draw_orders=[],
                checks=checks,
                errors=errors,
                warnings=warnings,
            )

        is_kml22 = root.tag == f"{{{KML_NS}}}kml"
        checks["valid_kml_xml"] = is_kml22
        if not is_kml22:
            errors.append(
                f"Expected KML 2.2 root element '{{{KML_NS}}}kml', got {root.tag!r}."
            )

        overlays = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "GroundOverlay"]
        tile_count = len(overlays)
        tile_cap_ok = 1 <= tile_count <= max_tiles
        checks["tile_count_within_cap"] = tile_cap_ok
        if not tile_cap_ok:
            errors.append(
                f"KMZ GroundOverlay tile count {tile_count} outside allowed range [1, {max_tiles}]."
            )

        all_files_exist = True
        all_sizes_ok = True
        all_px_ok = True
        all_rgb_ok = True
        all_baseline_ok = True
        all_latlon_ok = True
        all_draw_order_ok = True
        all_rotation_ok = True
        non_blank_found = False

        for idx, ov in enumerate(overlays):
            href = _find_kml_child_text(ov, "href")
            if not href or href not in names:
                all_files_exist = False
                errors.append(f"GroundOverlay[{idx}] references missing ZIP entry {href!r}.")
                continue

            tile_bytes = zf.read(href)
            tb_len = len(tile_bytes)
            max_bytes = max(max_bytes, tb_len)
            if tb_len == 0 or tb_len > max_tile_bytes:
                all_sizes_ok = False
                errors.append(
                    f"Tile {href!r} byte size {tb_len:,} exceeds limit {max_tile_bytes:,} (or is 0)."
                )

            # Strictly baseline JPEG check (SOF0 present, NO progressive SOF2)
            has_sof0, has_sof2, sof_list = _scan_jpeg_sof_markers(tile_bytes)
            if not has_sof0 or has_sof2 or any(m != 0xC0 for m in sof_list):
                all_baseline_ok = False
                errors.append(
                    f"Tile {href!r} is NOT strictly baseline JPEG (SOF0={has_sof0}, SOF2={has_sof2}, "
                    f"markers={[hex(m) for m in sof_list]})."
                )

            try:
                with Image.open(io.BytesIO(tile_bytes)) as img:
                    img.load()
                    w, h = img.size
                    max_w = max(max_w, w)
                    max_h = max(max_h, h)
                    if img.format != "JPEG":
                        all_baseline_ok = False
                        errors.append(f"Tile {href!r} Pillow format={img.format!r}, expected 'JPEG'.")
                    if img.info.get("progressive"):
                        all_baseline_ok = False
                        errors.append(f"Tile {href!r} flagged progressive by Pillow.")
                    if w > max_px or h > max_px or w <= 0 or h <= 0:
                        all_px_ok = False
                        errors.append(
                            f"Tile {href!r} dimensions {w}x{h} exceed Garmin cap {max_px}x{max_px}."
                        )
                    if img.mode != "RGB":
                        all_rgb_ok = False
                        errors.append(f"Tile {href!r} mode={img.mode!r}, expected 'RGB'.")
                    if _image_has_non_constant_pixels(img):
                        non_blank_found = True
            except Exception as exc:
                all_baseline_ok = False
                errors.append(f"Tile {href!r} failed Pillow decode: {exc}")

            # LatLonBox validation
            llbox = next((el for el in ov.iter() if el.tag.rsplit("}", 1)[-1] == "LatLonBox"), None)
            if llbox is None:
                all_latlon_ok = False
                errors.append(f"GroundOverlay[{idx}] ({href!r}) missing <LatLonBox>.")
            else:
                try:
                    north = float(_find_kml_child_text(llbox, "north") or "")
                    south = float(_find_kml_child_text(llbox, "south") or "")
                    east = float(_find_kml_child_text(llbox, "east") or "")
                    west = float(_find_kml_child_text(llbox, "west") or "")
                    if not (-90.0 <= south < north <= 90.0 and -180.0 <= west < east <= 180.0):
                        raise ValueError(f"invalid box N={north}, S={south}, E={east}, W={west}")
                    min_west = min(min_west, west)
                    min_south = min(min_south, south)
                    max_east = max(max_east, east)
                    max_north = max(max_north, north)
                except Exception as exc:
                    all_latlon_ok = False
                    errors.append(f"GroundOverlay[{idx}] ({href!r}) invalid <LatLonBox>: {exc}")

                rot_txt = _find_kml_child_text(llbox, "rotation")
                if rot_txt is not None:
                    try:
                        rot = float(rot_txt)
                        if not (-180.0 <= rot <= 180.0):
                            all_rotation_ok = False
                            errors.append(f"GroundOverlay[{idx}] rotation {rot} outside [-180, 180].")
                        elif abs(rot) > 1e-6:
                            warnings.append(
                                f"GroundOverlay[{idx}] uses nonzero rotation={rot}; upright north-up tiles preferred."
                            )
                    except ValueError:
                        all_rotation_ok = False
                        errors.append(f"GroundOverlay[{idx}] non-numeric rotation {rot_txt!r}.")

            # drawOrder check (50..75 so custom map draws over Garmin basemap while routes stay on top)
            do_txt = _find_kml_child_text(ov, "drawOrder")
            if do_txt is None:
                all_draw_order_ok = False
                errors.append(f"GroundOverlay[{idx}] ({href!r}) missing <drawOrder>.")
            else:
                try:
                    do_val = int(do_txt)
                    draw_orders.append(do_val)
                    if not (50 <= do_val <= 75):
                        all_draw_order_ok = False
                        errors.append(
                            f"GroundOverlay[{idx}] ({href!r}) drawOrder={do_val} outside [50, 75]."
                        )
                except ValueError:
                    all_draw_order_ok = False
                    errors.append(f"GroundOverlay[{idx}] ({href!r}) invalid drawOrder={do_txt!r}.")

        checks["tiles_exist_in_zip"] = all_files_exist
        checks["tile_bytes_within_cap"] = all_sizes_ok
        checks["tile_pixels_within_cap"] = all_px_ok
        checks["tile_mode_rgb"] = all_rgb_ok
        checks["tile_baseline_jpeg_only"] = all_baseline_ok
        checks["valid_latlonbox"] = all_latlon_ok
        checks["draw_order_50_75"] = all_draw_order_ok
        checks["valid_rotation"] = all_rotation_ok
        checks["tile_non_blank"] = non_blank_found
        if tile_count > 0 and not non_blank_found:
            errors.append("All KMZ tiles appear blank/single-color.")

    union_bounds = (
        (min_west, min_south, max_east, max_north)
        if min_west < max_east and min_south < max_north
        else None
    )
    if expected_bounds is not None and union_bounds is not None:
        ew, es, ee, en = expected_bounds
        uw, us, ue, un = union_bounds
        tol = 0.02
        bounds_ok = uw <= ew + tol and ue >= ee - tol and us <= es + tol and un >= en - tol
        checks["expected_bounds_match"] = bounds_ok
        if not bounds_ok:
            errors.append(
                f"KMZ union LatLonBox {union_bounds} does not cover expected WGS84 bounds {expected_bounds}."
            )

    return KMZVerificationReport(
        kmz_path=kmz_path,
        file_size_bytes=file_size,
        tile_count=tile_count,
        max_tiles_allowed=max_tiles,
        max_observed_px=(max_w, max_h),
        max_observed_tile_bytes=max_bytes,
        union_bounds=union_bounds,
        draw_orders=draw_orders,
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def assert_garmin_kmz_valid(path: Path | str, **kwargs: Any) -> KMZVerificationReport:
    """Run verify_garmin_kmz and raise AssertionError with full diagnostics on failure."""
    report = verify_garmin_kmz(path, **kwargs)
    if not report.passed:
        raise AssertionError(report.summary())
    return report


def extract_kmz_samples(
    path: Path | str,
    out_dir: Path | str,
    *,
    prefix: str | None = None,
) -> dict[str, Path]:
    """Extract a center KMZ tile plus a stitched north-up overview PNG for visual inspection."""
    src = Path(path)
    dst = Path(out_dir)
    dst.mkdir(parents=True, exist_ok=True)
    stem = prefix or src.stem

    outputs: dict[str, Path] = {}
    with zipfile.ZipFile(src, "r") as zf:
        root = ET.fromstring(zf.read("doc.kml"))
        overlays = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "GroundOverlay"]
        if not overlays:
            raise RuntimeError(f"No GroundOverlay entries in KMZ: {src}")

        entries: list[tuple[float, float, str]] = []
        for ov in overlays:
            href = _find_kml_child_text(ov, "href") or ""
            llbox = next((el for el in ov.iter() if el.tag.rsplit("}", 1)[-1] == "LatLonBox"), None)
            if not href or llbox is None:
                continue
            north = float(_find_kml_child_text(llbox, "north") or "0")
            west = float(_find_kml_child_text(llbox, "west") or "0")
            entries.append((north, west, href))

        # Group rows by distinct north coordinate (descending -> north at top), columns by west (ascending)
        row_keys = sorted({round(n, 5) for n, _, _ in entries}, reverse=True)
        col_keys = sorted({round(w, 5) for _, w, _ in entries})
        grid: dict[tuple[int, int], Image.Image] = {}
        row_heights: dict[int, int] = {}
        col_widths: dict[int, int] = {}

        for n, w, href in entries:
            ri = row_keys.index(round(n, 5))
            ci = col_keys.index(round(w, 5))
            img = Image.open(io.BytesIO(zf.read(href))).convert("RGB")
            grid[(ri, ci)] = img
            row_heights[ri] = max(row_heights.get(ri, 0), img.height)
            col_widths[ci] = max(col_widths.get(ci, 0), img.width)

        center_ri = len(row_keys) // 2
        center_ci = len(col_keys) // 2
        if (center_ri, center_ci) in grid:
            center_out = dst / f"{stem}_center_tile.png"
            grid[(center_ri, center_ci)].save(center_out, format="PNG")
            outputs["center_tile"] = center_out

        total_w = sum(col_widths.get(ci, 0) for ci in range(len(col_keys)))
        total_h = sum(row_heights.get(ri, 0) for ri in range(len(row_keys)))
        stitched = Image.new("RGB", (total_w, total_h), (255, 255, 255))
        y_off = 0
        for ri in range(len(row_keys)):
            x_off = 0
            for ci in range(len(col_keys)):
                if (ri, ci) in grid:
                    stitched.paste(grid[(ri, ci)], (x_off, y_off))
                x_off += col_widths.get(ci, 0)
            y_off += row_heights.get(ri, 0)

        # Downscale stitched overview if huge so inspection PNG stays crisp and fast to view
        max_side = 1800
        if max(stitched.width, stitched.height) > max_side:
            stitched.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        overview_out = dst / f"{stem}_stitched.png"
        stitched.save(overview_out, format="PNG")
        outputs["stitched"] = overview_out

    return outputs


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify GridForge Phase 3 offline exports (.mbtiles or .kmz)."
    )
    parser.add_argument("archive", type=Path, help="Path to .mbtiles or .kmz archive.")
    parser.add_argument("--format", dest="tile_format", choices=("png", "jpg"), default="png")
    parser.add_argument("--max-tiles", type=int, default=100, help="Garmin KMZ max tiles cap.")
    parser.add_argument("--max-px", type=int, default=1024, help="Garmin KMZ max tile width/height.")
    parser.add_argument(
        "--max-tile-bytes",
        type=int,
        default=3_000_000,
        help="Garmin KMZ max single JPEG byte size.",
    )
    parser.add_argument(
        "--extract-dir",
        type=Path,
        default=None,
        help="Optional directory to extract sample tile + stitched preview PNGs.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    suffix = args.archive.suffix.lower()

    if suffix == ".mbtiles":
        report: Any = verify_mbtiles(args.archive, expected_format=args.tile_format)
        if args.extract_dir is not None:
            extract_mbtiles_preview(args.archive, args.extract_dir)
    elif suffix == ".kmz":
        report = verify_garmin_kmz(
            args.archive,
            max_tiles=args.max_tiles,
            max_px=args.max_px,
            max_tile_bytes=args.max_tile_bytes,
        )
        if args.extract_dir is not None:
            extract_kmz_samples(args.archive, args.extract_dir)
    else:
        print(f"Unsupported archive suffix {suffix!r}; expected .mbtiles or .kmz.", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
