"""Air-gapped / offline local OSM PBF or OSM XML vector source using osmium extract."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from gridforge.config import SheetLayout

logger = logging.getLogger(__name__)


class LocalPBFSource:
    """Offline OpenStreetMap source extracting a bounding box from a local ``.osm.pbf`` or ``.osm``."""

    name: str = "osm-local-pbf"
    attribution: str = "Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright"

    def __init__(self, path: Path | str, attribution: str | None = None) -> None:
        self.path = Path(path)
        if attribution is not None:
            self.attribution = attribution

    def fetch_osm(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Extract ``layout.wgs84_fetch_bbox`` from the local OSM archive using ``osmium extract``."""
        work_dir.mkdir(parents=True, exist_ok=True)
        source_path = self.path.expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Local OSM PBF/XML file does not exist: {source_path}")

        bbox = layout.wgs84_fetch_bbox
        bbox_str = f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f}"
        dst_pbf = work_dir / "osm_extract.osm.pbf"

        cmd = [
            "osmium",
            "extract",
            "-b",
            bbox_str,
            "--strategy=complete_ways",
            str(source_path),
            "-o",
            str(dst_pbf),
            "--overwrite",
        ]
        logger.info("Extracting OSM bbox %s from %s", bbox_str, source_path)
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"osmium extract failed (exit {proc.returncode}):\n"
                f"cmd: {' '.join(cmd)}\n"
                f"stderr: {proc.stderr.strip()}"
            )

        if not dst_pbf.is_file() or dst_pbf.stat().st_size == 0:
            raise RuntimeError(f"osmium extract produced no output at {dst_pbf}")

        return dst_pbf
