"""Memory-safe disk-to-disk offline regional data pack builder (< 300 MB peak RAM)."""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from osgeo import gdal, ogr, osr

from gridforge.config import (
    AUOverlayKind,
    BBox,
    FrameMarginsMM,
    PackManifest,
    SheetLayout,
    SheetSpec,
)
from gridforge.geo.crs import resolve_utm_zone
from gridforge.packs.catalog import RegionPreset, resolve_region

logger = logging.getLogger(__name__)

DEFAULT_AU_PBF_URL: str = (
    "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf"
)
DEA_SRTM_1S_URL: str = (
    "https://dea-public-data.s3.ap-southeast-2.amazonaws.com/"
    "projects/elevation/ga_srtm_dem1sv1_0/dems1sv1_0.tif"
)
GA_DEM_ATTRIBUTION: str = (
    "Elevation data: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)"
)
OSM_ATTRIBUTION: str = (
    "Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright"
)
COPERNICUS_ATTRIBUTION: str = (
    "Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and "
    "Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; "
    "all rights reserved."
)

_GDAL_CURL_ENV: dict[str, str] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.vrt",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_CACHEMAX": "128",
    "OMP_THREAD_LIMIT": "1",
}

_ALL_AU_OVERLAYS: tuple[AUOverlayKind, ...] = (
    AUOverlayKind.AU_HYDRO,
    AUOverlayKind.AU_PARKS,
    AUOverlayKind.AU_INFRA,
    AUOverlayKind.AU_AVIATION,
)


def _run_cmd(
    cmd: list[str],
    *,
    step: str,
    extra_env: Mapping[str, str] | None = None,
) -> None:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-2500:]}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}"
        )


def _default_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).resolve()
    env_dir = os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
    return Path(env_dir).resolve()


def _stream_download_file(url: str, dest: Path, *, chunk_bytes: int = 1_048_576) -> Path:
    """Stream a large file to disk in fixed-size chunks (1 MiB) with atomic rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        logger.info("Using cached download %s (%d bytes)", dest, dest.stat().st_size)
        return dest

    part_path = dest.with_suffix(dest.suffix + ".part")
    if part_path.exists():
        part_path.unlink()

    headers = {"User-Agent": "gridforge/0.1 (+https://github.com/gridforge/gridforge)"}
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with part_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=chunk_bytes):
                        if chunk:
                            fh.write(chunk)
        part_path.replace(dest)
    except Exception:
        if part_path.exists():
            part_path.unlink()
        raise

    return dest


def _extract_osm_pbf(
    bbox: BBox,
    out_pbf: Path,
    *,
    source_pbf: Path | None,
    cache_dir: Path,
) -> Path:
    """Stream-download Geofabrik Australia PBF if needed and clip bbox with osmium."""
    if source_pbf is not None:
        src = Path(source_pbf).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Source OSM PBF/XML not found: '{src}'")
    else:
        url = os.environ.get("GRIDFORGE_AU_PBF_URL", DEFAULT_AU_PBF_URL).strip()
        filename = Path(url.split("?")[0]).name or "australia-latest.osm.pbf"
        src = _stream_download_file(url, cache_dir / "geofabrik" / filename)

    out_pbf.parent.mkdir(parents=True, exist_ok=True)
    bbox_arg = f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f}"
    _run_cmd(
        [
            "osmium",
            "extract",
            "-b",
            bbox_arg,
            "--strategy=complete_ways",
            str(src),
            "-o",
            str(out_pbf),
            "--overwrite",
        ],
        step="osmium extract pack OSM",
    )
    return out_pbf


def _raster_wgs84_bounds(tif_path: Path) -> BBox | None:
    """Return WGS84 BBox of a local raster, or None if SRS cannot be determined."""
    gdal.UseExceptions()
    osr.UseExceptions()
    ds = gdal.Open(str(tif_path))
    if ds is None:
        return None
    try:
        w, h = ds.RasterXSize, ds.RasterYSize
        gt = ds.GetGeoTransform()
        wkt = ds.GetProjection()
    finally:
        ds = None

    xs = [gt[0], gt[0] + w * gt[1]]
    ys = [gt[3], gt[3] + h * gt[5]]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    if not wkt:
        return BBox(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)

    src_srs = osr.SpatialReference(wkt=wkt)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(4326)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    if src_srs.IsSame(dst_srs):
        return BBox(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)

    ct = osr.CoordinateTransformation(src_srs, dst_srs)
    corners = [
        ct.TransformPoint(min_x, min_y),
        ct.TransformPoint(max_x, min_y),
        ct.TransformPoint(min_x, max_y),
        ct.TransformPoint(max_x, max_y),
    ]
    lons = [p[0] for p in corners]
    lats = [p[1] for p in corners]
    return BBox(min_x=min(lons), min_y=min(lats), max_x=max(lons), max_y=max(lats))


def _make_tile_layout(tile_bbox: BBox) -> SheetLayout:
    """Build a lightweight SheetLayout representing a WGS84 spatial tile for overlay fetching."""
    c_lon = (tile_bbox.min_x + tile_bbox.max_x) / 2.0
    c_lat = (tile_bbox.min_y + tile_bbox.max_y) / 2.0
    zone, hemi, epsg, gzd = resolve_utm_zone(c_lat, c_lon)
    spec = SheetSpec(center_lat=c_lat, center_lon=c_lon, scale=25000)

    from pyproj import Transformer

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    cx, cy = to_utm.transform(c_lon, c_lat)
    xs, ys = to_utm.transform(
        [tile_bbox.min_x, tile_bbox.max_x, tile_bbox.min_x, tile_bbox.max_x],
        [tile_bbox.min_y, tile_bbox.min_y, tile_bbox.max_y, tile_bbox.max_y],
    )
    utm_bbox = BBox(
        min_x=float(min(xs)),
        min_y=float(min(ys)),
        max_x=float(max(xs)),
        max_y=float(max(ys)),
    )
    return SheetLayout(
        spec=spec,
        utm_epsg=epsg,
        utm_zone=zone,
        utm_hemisphere=hemi,
        gzd=gzd,
        center_easting=float(cx),
        center_northing=float(cy),
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=FrameMarginsMM(),
        frame_x_mm=12.0,
        frame_y_mm=14.0,
        frame_width_mm=186.0,
        frame_height_mm=235.0,
        utm_bbox=utm_bbox,
        utm_fetch_bbox=utm_bbox,
        wgs84_bbox=tile_bbox,
        wgs84_fetch_bbox=tile_bbox,
    )


def _extract_dem(
    bbox: BBox,
    out_dem: Path,
    *,
    source_dem: Path | None,
    dem_tier: Literal["srtm-1s", "glo30", "au-elvis"],
    cache_dir: Path,
) -> tuple[Path, str, str]:
    """Stream-clip DEM disk-to-disk into compressed tiled Float32 GeoTIFF.

    Returns:
        (out_dem, vertical_datum, attribution)
    """
    out_dem.parent.mkdir(parents=True, exist_ok=True)
    if out_dem.exists():
        out_dem.unlink()

    gdal_opts = [
        "-co",
        "COMPRESS=DEFLATE",
        "-co",
        "PREDICTOR=3",
        "-co",
        "TILED=YES",
        "-co",
        "BIGTIFF=IF_SAFER",
    ]

    if source_dem is not None:
        src = Path(source_dem).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Source DEM file not found: '{src}'")

        clip_bbox = bbox
        src_bounds = _raster_wgs84_bounds(src)
        if src_bounds is not None:
            inter_w = max(bbox.min_x, src_bounds.min_x)
            inter_s = max(bbox.min_y, src_bounds.min_y)
            inter_e = min(bbox.max_x, src_bounds.max_x)
            inter_n = min(bbox.max_y, src_bounds.max_y)
            if inter_w < inter_e and inter_s < inter_n:
                clip_bbox = BBox(min_x=inter_w, min_y=inter_s, max_x=inter_e, max_y=inter_n)

        _run_cmd(
            [
                "gdal_translate",
                "-ot",
                "Float32",
                "-projwin",
                f"{clip_bbox.min_x:.6f}",
                f"{clip_bbox.max_y:.6f}",
                f"{clip_bbox.max_x:.6f}",
                f"{clip_bbox.min_y:.6f}",
                "-projwin_srs",
                "EPSG:4326",
                *gdal_opts,
                str(src),
                str(out_dem),
            ],
            step="gdal_translate local source DEM for pack",
            extra_env=_GDAL_CURL_ENV,
        )
        datum = "EGM2008" if dem_tier == "glo30" else "AHD"
        attr = COPERNICUS_ATTRIBUTION if dem_tier == "glo30" else GA_DEM_ATTRIBUTION
        return (out_dem, datum, attr)

    if dem_tier == "srtm-1s":
        url = os.environ.get("GRIDFORGE_DEA_SRTM_URL", DEA_SRTM_1S_URL).strip()
        vsicurl_src = f"/vsicurl/{url}" if url.startswith(("http://", "https://")) else url
        _run_cmd(
            [
                "gdal_translate",
                "-ot",
                "Float32",
                "-projwin",
                f"{bbox.min_x:.6f}",
                f"{bbox.max_y:.6f}",
                f"{bbox.max_x:.6f}",
                f"{bbox.min_y:.6f}",
                "-projwin_srs",
                "EPSG:4326",
                *gdal_opts,
                vsicurl_src,
                str(out_dem),
            ],
            step="gdal_translate DEA GA SRTM 1s for pack",
            extra_env=_GDAL_CURL_ENV,
        )
        return (out_dem, "AHD", GA_DEM_ATTRIBUTION)

    if dem_tier == "glo30":
        from gridforge.sources.dem_copernicus import CopernicusGLO30Source

        with tempfile.TemporaryDirectory(prefix="gf_pack_glo30_") as tmp_s:
            tmp_dir = Path(tmp_s)
            src_glo30 = CopernicusGLO30Source(cache_dir=cache_dir / "copernicus")
            raw_tif = src_glo30.fetch_dem(_make_tile_layout(bbox), tmp_dir)
            _run_cmd(
                ["gdal_translate", "-ot", "Float32", *gdal_opts, str(raw_tif), str(out_dem)],
                step="gdal_translate GLO-30 DEM compression for pack",
                extra_env=_GDAL_CURL_ENV,
            )
            return (out_dem, "EGM2008", src_glo30.attribution)

    if dem_tier == "au-elvis":
        # Guard WCS 5m pixel size: use GA 5m LiDAR only on compact tactical packs <= 1 deg.
        if bbox.width <= 1.0 and bbox.height <= 1.0:
            try:
                from gridforge.sources.dem_au_elvis import AUElvisDEMSource

                with tempfile.TemporaryDirectory(prefix="gf_pack_auelvis_") as tmp_s:
                    tmp_dir = Path(tmp_s)
                    src_au = AUElvisDEMSource(cache_dir=cache_dir / "au_elvis")
                    raw_tif = src_au.fetch_dem(_make_tile_layout(bbox), tmp_dir)
                    _run_cmd(
                        ["gdal_translate", "-ot", "Float32", *gdal_opts, str(raw_tif), str(out_dem)],
                        step="gdal_translate AU Elvis DEM compression for pack",
                        extra_env=_GDAL_CURL_ENV,
                    )
                    return (out_dem, src_au.vertical_datum, src_au.attribution)
            except Exception as exc:
                logger.warning("au-elvis pack DEM failed (%s); falling back to DEA SRTM 1s", exc)

        return _extract_dem(
            bbox,
            out_dem,
            source_dem=None,
            dem_tier="srtm-1s",
            cache_dir=cache_dir,
        )

    raise ValueError(f"Unsupported dem_tier '{dem_tier}'. Use 'srtm-1s', 'glo30', or 'au-elvis'.")


def _list_gpkg_layer_counts(gpkg_path: Path) -> list[tuple[str, int]]:
    ogr.UseExceptions()
    ds = ogr.Open(str(gpkg_path))
    if ds is None:
        return []
    try:
        out: list[tuple[str, int]] = []
        for idx in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(idx)
            out.append((lyr.GetName(), int(lyr.GetFeatureCount())))
        return out
    finally:
        ds = None


def _iter_spatial_tiles(bbox: BBox, max_deg: float) -> list[BBox]:
    """Partition a WGS84 BBox into bounded spatial tiles of at most max_deg x max_deg."""
    step = max(0.1, float(max_deg))
    nx = max(1, int(math.ceil(bbox.width / step)))
    ny = max(1, int(math.ceil(bbox.height / step)))
    tiles: list[BBox] = []
    for ix in range(nx):
        w = bbox.min_x + (bbox.width * ix / nx)
        e = bbox.min_x + (bbox.width * (ix + 1) / nx)
        for iy in range(ny):
            s = bbox.min_y + (bbox.height * iy / ny)
            n = bbox.min_y + (bbox.height * (iy + 1) / ny)
            tiles.append(BBox(min_x=w, min_y=s, max_x=e, max_y=n))
    return tiles


def _append_gpkg_layers_wgs84(
    src_gpkg: Path,
    dst_gpkg: Path,
    *,
    spat_bbox: BBox | None = None,
) -> int:
    """Append all non-empty layers from src_gpkg into dst_gpkg in EPSG:4326. Returns features added."""
    layers = _list_gpkg_layer_counts(src_gpkg)
    added_layers = 0
    existing_layers = {name for name, _ in _list_gpkg_layer_counts(dst_gpkg)} if dst_gpkg.is_file() else set()

    for layer_name, count in layers:
        if count <= 0:
            continue
        cmd = ["ogr2ogr", "-f", "GPKG", "-t_srs", "EPSG:4326"]
        if spat_bbox is not None:
            cmd.extend(
                [
                    "-spat",
                    f"{spat_bbox.min_x:.6f}",
                    f"{spat_bbox.min_y:.6f}",
                    f"{spat_bbox.max_x:.6f}",
                    f"{spat_bbox.max_y:.6f}",
                    "-spat_srs",
                    "EPSG:4326",
                    "-clipsrc",
                    f"{spat_bbox.min_x:.6f}",
                    f"{spat_bbox.min_y:.6f}",
                    f"{spat_bbox.max_x:.6f}",
                    f"{spat_bbox.max_y:.6f}",
                ]
            )
        if dst_gpkg.is_file():
            cmd.append("-update")
            if layer_name in existing_layers:
                cmd.append("-append")
        cmd.extend([str(dst_gpkg), str(src_gpkg), layer_name, "-nln", layer_name])
        _run_cmd(cmd, step=f"ogr2ogr merge overlay layer {layer_name}", extra_env=_GDAL_CURL_ENV)
        existing_layers.add(layer_name)
        added_layers += 1

    return added_layers


def _extract_au_overlays(
    bbox: BBox,
    overlays_dir: Path,
    *,
    local_overlay_overrides: Mapping[str | AUOverlayKind, Path | str] | None,
    overlay_tile_deg: float,
) -> tuple[dict[str, str], list[str]]:
    """Fetch or clip Australian regional overlays in bounded low-RAM spatial chunks."""
    overlays_dir.mkdir(parents=True, exist_ok=True)
    normalized_overrides: dict[str, Path] = {}
    if local_overlay_overrides:
        for k, v in local_overlay_overrides.items():
            key_str = k.value if isinstance(k, AUOverlayKind) else str(k)
            normalized_overrides[key_str] = Path(v).resolve()

    overlay_rel_paths: dict[str, str] = {}
    attributions: list[str] = []

    default_attrs: dict[str, str] = {
        "au-hydro": "Surface hydrology: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)",
        "au-parks": "Protected areas: CAPAD 2025 © Commonwealth of Australia (DCCEEW) (CC BY 4.0)",
        "au-infra": "Energy infrastructure: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)",
        "au-aviation": "Aviation & cultural features: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)",
    }

    cloud_sources: dict[str, Any] = {}
    need_cloud = [k for k in _ALL_AU_OVERLAYS if k.value not in normalized_overrides]
    if need_cloud:
        try:
            from gridforge.sources.overlays_au import build_au_overlay_sources

            cloud_sources = build_au_overlay_sources(need_cloud)
        except Exception as exc:
            logger.warning("Unable to import AU cloud overlay sources (%s); skipping cloud overlays", exc)

    spatial_tiles = _iter_spatial_tiles(bbox, overlay_tile_deg)

    for kind in _ALL_AU_OVERLAYS:
        key = kind.value
        slug = key.replace("-", "_")
        target_gpkg = overlays_dir / f"{slug}.gpkg"
        if target_gpkg.exists():
            target_gpkg.unlink()

        if key in normalized_overrides:
            src_local = normalized_overrides[key]
            if not src_local.is_file():
                logger.warning("Local overlay override missing for %s: %s", key, src_local)
                continue
            try:
                _append_gpkg_layers_wgs84(src_local, target_gpkg, spat_bbox=bbox)
            except Exception as exc:
                logger.warning("Failed clipping local overlay %s (%s); skipping", key, exc)
                continue
            if target_gpkg.is_file() and any(c > 0 for _, c in _list_gpkg_layer_counts(target_gpkg)):
                overlay_rel_paths[key] = f"overlays/{slug}.gpkg"
                attributions.append(default_attrs[key])
            elif target_gpkg.exists():
                target_gpkg.unlink()
            continue

        src = cloud_sources.get(key)
        if src is None:
            continue

        for tile_bbox in spatial_tiles:
            tile_layout = _make_tile_layout(tile_bbox)
            with tempfile.TemporaryDirectory(prefix=f"gf_pack_{slug}_") as tile_tmp:
                try:
                    chunk_gpkg = src.fetch_overlay(tile_layout, Path(tile_tmp))
                except Exception as exc:
                    logger.warning("Overlay %s tile %s failed (%s); continuing", key, tile_bbox, exc)
                    continue
                if chunk_gpkg is not None and Path(chunk_gpkg).is_file():
                    try:
                        _append_gpkg_layers_wgs84(Path(chunk_gpkg), target_gpkg)
                    except Exception as exc:
                        logger.warning("Overlay %s merge failed (%s); continuing", key, exc)

        if target_gpkg.is_file() and any(c > 0 for _, c in _list_gpkg_layer_counts(target_gpkg)):
            overlay_rel_paths[key] = f"overlays/{slug}.gpkg"
            attr = str(getattr(src, "attribution", "") or default_attrs[key])
            if attr and attr not in attributions:
                attributions.append(attr)
        elif target_gpkg.exists():
            target_gpkg.unlink()

    return (overlay_rel_paths, attributions)


def _compute_dir_size_bytes(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def _slugify_pack_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return slug or "pack"


def build_offline_pack(
    region_or_bbox: str,
    output_dir: Path | str,
    *,
    name: str | None = None,
    include_osm: bool = True,
    include_dem: bool = True,
    include_overlays: bool = True,
    source_pbf: Path | str | None = None,
    source_dem: Path | str | None = None,
    local_overlay_overrides: Mapping[str | AUOverlayKind, Path | str] | None = None,
    dem_tier: Literal["srtm-1s", "glo30", "au-elvis"] = "srtm-1s",
    cache_dir: Path | None = None,
    overlay_tile_deg: float = 1.0,
) -> PackManifest:
    """Build a self-contained, air-gapped offline regional pack streaming disk-to-disk."""
    preset: RegionPreset = resolve_region(region_or_bbox)
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    resolved_cache = _default_cache_dir(cache_dir)

    pack_id = _slugify_pack_id(out_dir.name if preset.category == "custom" else preset.region_id)
    pack_name = name or preset.name
    attributions: list[str] = []

    osm_rel: str | None = None
    if include_osm:
        _extract_osm_pbf(
            preset.bbox_wgs84,
            out_dir / "osm.osm.pbf",
            source_pbf=Path(source_pbf) if source_pbf is not None else None,
            cache_dir=resolved_cache,
        )
        osm_rel = "osm.osm.pbf"
        attributions.append(OSM_ATTRIBUTION)

    dem_rel: str | None = None
    vertical_datum = "AHD"
    dem_attr: str | None = None
    if include_dem:
        _, vertical_datum, dem_attr = _extract_dem(
            preset.bbox_wgs84,
            out_dir / "dem.tif",
            source_dem=Path(source_dem) if source_dem is not None else None,
            dem_tier=dem_tier,
            cache_dir=resolved_cache,
        )
        dem_rel = "dem.tif"
        if dem_attr and dem_attr not in attributions:
            attributions.append(dem_attr)

    overlays_rel: dict[str, str] = {}
    if include_overlays:
        overlays_rel, ov_attrs = _extract_au_overlays(
            preset.bbox_wgs84,
            out_dir / "overlays",
            local_overlay_overrides=local_overlay_overrides,
            overlay_tile_deg=overlay_tile_deg,
        )
        for attr in ov_attrs:
            if attr not in attributions:
                attributions.append(attr)

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_path = out_dir / "pack.json"

    # Compute total pack size across all data artifacts + manifest.
    manifest = PackManifest(
        pack_id=pack_id,
        name=pack_name,
        region=preset.region_id,
        bbox_wgs84=preset.bbox_wgs84,
        utm_zones=preset.utm_zones,
        dem_path=dem_rel,
        vertical_datum=vertical_datum,
        dem_attribution=dem_attr,
        osm_pbf_path=osm_rel,
        overlays=overlays_rel,
        attributions=attributions,
        created_at=created_at,
        size_bytes=0,
    )
    payload = manifest.model_dump_json(indent=2)
    artifact_bytes = sum(
        p.stat().st_size
        for p in out_dir.rglob("*")
        if p.is_file() and p.resolve() != manifest_path.resolve()
    )
    manifest = manifest.model_copy(
        update={"size_bytes": artifact_bytes + len(payload.encode("utf-8"))}
    )
    tmp_manifest = out_dir / "pack.json.tmp"
    tmp_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp_manifest.replace(manifest_path)

    return manifest


def load_pack_manifest(pack_dir: Path | str) -> PackManifest:
    """Load and validate ``pack.json`` from an offline pack directory."""
    resolved = Path(pack_dir).resolve()
    manifest_path = resolved / "pack.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Offline pack manifest not found at '{manifest_path}'.")
    return PackManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def discover_packs(packs_root: Path | str) -> list[PackManifest]:
    """Discover all valid offline packs under ``packs_root``, sorted by ``pack_id``."""
    root = Path(packs_root)
    if not root.is_dir():
        return []

    found: list[PackManifest] = []
    seen_ids: set[str] = set()

    candidates: list[Path] = []
    if (root / "pack.json").is_file():
        candidates.append(root)
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "pack.json").is_file():
            candidates.append(child)

    for pack_dir in candidates:
        try:
            manifest = load_pack_manifest(pack_dir)
        except Exception as exc:
            logger.warning("Skipping invalid offline pack in '%s': %s", pack_dir, exc)
            continue
        if manifest.pack_id not in seen_ids:
            seen_ids.add(manifest.pack_id)
            found.append(manifest)

    return sorted(found, key=lambda m: m.pack_id)
