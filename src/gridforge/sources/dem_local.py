"""Air-gapped / offline local GeoTIFF or VRT DEM source."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from gridforge.config import SheetLayout
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

DEM_EXTENSIONS: tuple[str, ...] = (".tif", ".tiff", ".vrt")


class LocalDEMSource:
    """Offline elevation source backed by a local GeoTIFF/VRT file or tile directory."""

    name: str = "local-dem"
    attribution: str = "Elevation data: Local DEM"

    def __init__(self, path: Path | str, attribution: str | None = None) -> None:
        self.path = Path(path)
        if attribution is not None:
            self.attribution = attribution

    def _discover_rasters(self, directory: Path) -> list[Path]:
        direct = sorted(
            p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in DEM_EXTENSIONS
        )
        if direct:
            return direct
        return sorted(
            p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in DEM_EXTENSIONS
        )

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Resolve a local DEM file or mosaic a directory of tiles into a VRT.

        Preserves the source CRS so ``terrain.build_terrain_products`` can warp directly
        to ``EPSG:{layout.utm_epsg}`` covering ``layout.utm_fetch_bbox`` in one pass.
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        source_path = self.path.expanduser().resolve()

        if not source_path.exists():
            raise FileNotFoundError(f"Local DEM path does not exist: {source_path}")

        if source_path.is_file():
            logger.info("Using local DEM file: %s", source_path)
            return source_path

        if source_path.is_dir():
            rasters = self._discover_rasters(source_path)
            if not rasters:
                raise FileNotFoundError(
                    f"No DEM files ({', '.join(DEM_EXTENSIONS)}) found in directory: {source_path}"
                )
            vrt_path = work_dir / "dem_local.vrt"
            if vrt_path.exists():
                vrt_path.unlink()
            cmd = ["gdalbuildvrt", str(vrt_path), *(str(r) for r in rasters)]
            logger.info("Building local DEM VRT from %d raster(s) in %s", len(rasters), source_path)
            proc = subprocess.run(
                cmd,
                env=build_safe_subprocess_env(allow_network=False),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"gdalbuildvrt failed for local DEM directory {source_path} "
                    f"(exit {proc.returncode}): {proc.stderr.strip()}"
                )
            return vrt_path

        raise ValueError(f"Local DEM path is neither a file nor a directory: {source_path}")
