"""Generate deterministic synthetic offline DEM and overlay fixtures for tests.

Centered on Katoomba / Jamison Valley, Blue Mountains NSW (-33.732, 150.312, EPSG:32756).
Works both with ``osgeo.gdal`` (compressed GeoTIFF) and in pure Python (baseline GeoTIFF).
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent

# Encloses A4 & Letter 1:25,000 + 1:50,000 footprints with fetch buffer around (-33.732, 150.312)
MIN_LON = 150.22
MAX_LON = 150.41
MIN_LAT = -33.81
MAX_LAT = -33.65
WIDTH = 190
HEIGHT = 160
NODATA = -9999.0


def _elevation_m(lat: float, lon: float) -> float:
    """Smooth analytical Katoomba-like plateau, conical peak, and Jamison valley (~340–960 m)."""
    dx = (lon - 150.312) / 0.03
    dy = (lat - (-33.732)) / 0.03

    # Northern plateau sloping south into Jamison Valley
    plateau = 640.0 + 140.0 * math.tanh((dy + 0.25) * 1.4)
    # Conical peak near Mount Solitary (-33.726, 150.306)
    r_peak1 = ((lon - 150.306) / 0.015) ** 2 + ((lat - (-33.726)) / 0.014) ** 2
    peak1 = 230.0 * math.exp(-r_peak1)
    # Secondary ridge near Echo Ridge (-33.738, 150.320)
    r_peak2 = ((lon - 150.320) / 0.018) ** 2 + ((lat - (-33.738)) / 0.012) ** 2
    peak2 = 175.0 * math.exp(-r_peak2)
    # Deep valley trough to the south-west
    r_valley = ((lon - 150.295) / 0.024) ** 2 + ((lat - (-33.752)) / 0.020) ** 2
    valley = -190.0 * math.exp(-r_valley)
    # Gentle undulating ridge texture for realistic multi-directional hillshade
    undulation = 28.0 * math.sin(dx * 3.1) * math.cos(dy * 2.7)

    return float(round(plateau + peak1 + peak2 + valley + undulation, 2))


def _compute_grid() -> list[float]:
    px_w = (MAX_LON - MIN_LON) / WIDTH
    px_h = (MAX_LAT - MIN_LAT) / HEIGHT
    values: list[float] = []
    for row in range(HEIGHT):
        lat = MAX_LAT - (row + 0.5) * px_h
        for col in range(WIDTH):
            lon = MIN_LON + (col + 0.5) * px_w
            values.append(_elevation_m(lat, lon))
    return values


def _write_geotiff_pure_python(out_path: Path, values: list[float]) -> Path:
    """Write a valid uncompressed Little-Endian Float32 EPSG:4326 GeoTIFF with zero dependencies."""
    px_w = (MAX_LON - MIN_LON) / WIDTH
    px_h = (MAX_LAT - MIN_LAT) / HEIGHT

    pixel_bytes = struct.pack(f"<{len(values)}f", *values)
    pixel_scale_bytes = struct.pack("<3d", px_w, px_h, 0.0)
    tiepoint_bytes = struct.pack("<6d", 0.0, 0.0, 0.0, MIN_LON, MAX_LAT, 0.0)
    # GeoKeyDirectoryTag: Geographic WGS84 (EPSG:4326), RasterPixelIsArea
    geokeys = (
        1, 1, 0, 3,        # header: v1.1.0, 3 keys
        1024, 0, 1, 2,     # GTModelTypeGeoKey = ModelTypeGeographic
        1025, 0, 1, 1,     # GTRasterTypeGeoKey = RasterPixelIsArea
        2048, 0, 1, 4326,  # GeographicTypeGeoKey = GCS_WGS_84
    )
    geokey_bytes = struct.pack(f"<{len(geokeys)}H", *geokeys)
    nodata_bytes = b"-9999\x00"

    # Layout: 8-byte header | extra data blocks | pixel strip | IFD
    offset = 8
    scale_off = offset
    offset += len(pixel_scale_bytes)
    tie_off = offset
    offset += len(tiepoint_bytes)
    geokey_off = offset
    offset += len(geokey_bytes)
    nodata_off = offset
    offset += len(nodata_bytes)
    if offset % 4 != 0:
        pad = 4 - (offset % 4)
        nodata_pad = b"\x00" * pad
        offset += pad
    else:
        nodata_pad = b""

    strip_off = offset
    offset += len(pixel_bytes)
    ifd_off = offset

    # TIFF type codes: 2=ASCII, 3=SHORT, 4=LONG, 12=DOUBLE
    entries = [
        (256, 4, 1, WIDTH),                        # ImageWidth
        (257, 4, 1, HEIGHT),                       # ImageLength
        (258, 3, 1, 32),                           # BitsPerSample
        (259, 3, 1, 1),                            # Compression = None
        (262, 3, 1, 1),                            # PhotometricInterpretation = BlackIsZero
        (273, 4, 1, strip_off),                    # StripOffsets
        (277, 3, 1, 1),                            # SamplesPerPixel
        (278, 4, 1, HEIGHT),                       # RowsPerStrip
        (279, 4, 1, len(pixel_bytes)),             # StripByteCounts
        (339, 3, 1, 3),                            # SampleFormat = IEEE Float
        (33550, 12, 3, scale_off),                 # ModelPixelScaleTag
        (33922, 12, 6, tie_off),                   # ModelTiepointTag
        (34735, 3, len(geokeys), geokey_off),      # GeoKeyDirectoryTag
        (42113, 2, len(nodata_bytes), nodata_off), # GDAL_NODATA
    ]

    ifd_chunks = [struct.pack("<H", len(entries))]
    for tag, dtype, count, val in entries:
        if dtype == 3 and count == 1:
            val_bytes = struct.pack("<HH", val, 0)
        else:
            val_bytes = struct.pack("<I", val)
        ifd_chunks.append(struct.pack("<HHI", tag, dtype, count) + val_bytes)
    ifd_chunks.append(struct.pack("<I", 0))  # next IFD offset = 0

    header = struct.pack("<2sHI", b"II", 42, ifd_off)
    out_path.write_bytes(
        header
        + pixel_scale_bytes
        + tiepoint_bytes
        + geokey_bytes
        + nodata_bytes
        + nodata_pad
        + pixel_bytes
        + b"".join(ifd_chunks)
    )
    return out_path


def generate_synthetic_dem(out_path: Path | None = None) -> Path:
    """Create ``tests/fixtures/synthetic_dem.tif`` (DEFLATE-compressed if GDAL available)."""
    target = out_path or (FIXTURES_DIR / "synthetic_dem.tif")
    values = _compute_grid()

    try:
        from osgeo import gdal, osr  # type: ignore[import-untyped]

        gdal.UseExceptions()
        px_w = (MAX_LON - MIN_LON) / WIDTH
        px_h = (MAX_LAT - MIN_LAT) / HEIGHT
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(
            str(target),
            WIDTH,
            HEIGHT,
            1,
            gdal.GDT_Float32,
            options=["COMPRESS=DEFLATE", "TILED=YES"],
        )
        ds.SetGeoTransform((MIN_LON, px_w, 0.0, MAX_LAT, 0.0, -px_h))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())
        band = ds.GetRasterBand(1)
        band.SetNoDataValue(NODATA)
        band.WriteRaster(0, 0, WIDTH, HEIGHT, struct.pack(f"={len(values)}f", *values))
        band.FlushCache()
        ds.FlushCache()
        ds = None
        return target
    except ImportError:
        return _write_geotiff_pure_python(target, values)


if __name__ == "__main__":
    path = generate_synthetic_dem()
    print(f"Wrote synthetic DEM fixture: {path} ({path.stat().st_size} bytes)")
