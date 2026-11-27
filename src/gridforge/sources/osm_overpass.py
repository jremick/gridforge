"""On-demand OpenStreetMap Overpass API vector source with disk cache and mirror fallback."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Final, Sequence

import httpx

from gridforge.config import BBox, SheetLayout
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

PRIMARY_OVERPASS_URL: Final[str] = "https://overpass-api.de/api/interpreter"
FALLBACK_OVERPASS_URL: Final[str] = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
TERTIARY_OVERPASS_URL: Final[str] = "https://overpass.private.coffee/api/interpreter"
USER_AGENT: Final[str] = "gridforge/0.2 (+https://github.com/gridforge/gridforge)"
MAX_OVERPASS_RESPONSE_BYTES: Final[int] = 128 * 1024 * 1024  # 128 MiB hard disk-stream cap

_REMARK_RE = re.compile(rb"<remark\b[^>]*>(.*?)</remark>", flags=re.IGNORECASE | re.DOTALL)


def default_overpass_endpoints() -> list[str]:
    """Return deduplicated default Overpass planet endpoints honoring ``OVERPASS_URL``."""
    raw = [
        os.environ.get("OVERPASS_URL", PRIMARY_OVERPASS_URL).strip() or PRIMARY_OVERPASS_URL,
        FALLBACK_OVERPASS_URL,
        TERTIARY_OVERPASS_URL,
    ]
    endpoints: list[str] = []
    for url in raw:
        if url and url not in endpoints:
            endpoints.append(url)
    return endpoints


def round_enclosing_bbox(bbox: BBox, step_deg: float = 0.001) -> BBox:
    """Round a WGS84 bounding box outward to ``step_deg`` precision for cache stability."""
    inv = 1.0 / step_deg
    return BBox(
        min_x=math.floor(bbox.min_x * inv) / inv,
        min_y=math.floor(bbox.min_y * inv) / inv,
        max_x=math.ceil(bbox.max_x * inv) / inv,
        max_y=math.ceil(bbox.max_y * inv) / inv,
    )


def bbox_cache_key(bbox: BBox) -> str:
    """Compute a deterministic SHA256 cache key for an enclosing 0.001°-rounded bbox."""
    rounded = round_enclosing_bbox(bbox, step_deg=0.001)
    payload = f"v4:{rounded.min_x:.3f},{rounded.min_y:.3f},{rounded.max_x:.3f},{rounded.max_y:.3f}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def build_overpass_query(
    bbox: BBox,
    timeout_s: int = 60,
    maxsize_bytes: int = 536870912,
) -> str:
    """Build a GDAL-compatible cartographic Overpass QL query with single ID-sorted ``out body;``.

    Fetches all topographic/military features styled by GridForge (nodes, ways, and
    non-administrative multipolygons) while excluding statewide transit ``type=route``
    relations, ``boundary=administrative`` polygons, and untagged/non-cartographic urban
    clutter (addresses, individual trees, bollards) that overload public Overpass servers
    in dense metro areas like Sydney CBD. Never uses ``out skel qt;`` or split ``out`` blocks,
    preserving GDAL's strict ``nodes -> ways -> relations`` ascending-ID ordering.
    """
    s, w, n, e = bbox.min_y, bbox.min_x, bbox.max_y, bbox.max_x
    coords = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    return (
        f"[out:xml][timeout:{timeout_s}][maxsize:{maxsize_bytes}];\n"
        "(\n"
        f'  node["natural"~"^(peak|saddle|volcano|spring|water|cave_entrance|cliff|rock)$"]({coords});\n'
        f'  node["waterway"~"^(waterfall|rapids)$"]({coords});\n'
        f'  node["place"]({coords});\n'
        f'  node["highway"~"^(motorway_junction|ford)$"]({coords});\n'
        f'  node["ford"="yes"]({coords});\n'
        f'  node["barrier"~"^(gate|stile|kissing_gate|cattle_grid|lift_gate)$"]({coords});\n'
        f'  node["railway"~"^(station|halt|stop|tram_stop|level_crossing)$"]({coords});\n'
        f'  node["aeroway"]({coords});\n'
        f'  node["amenity"~"^(hospital|clinic|fire_station|police|school|college|university|place_of_worship|fuel|shelter)$"]({coords});\n'
        f'  node["emergency"]({coords});\n'
        f'  node["power"~"^(tower|pole|substation|plant|generator)$"]({coords});\n'
        f'  node["man_made"~"^(tower|mast|communications_tower|water_tower|lighthouse|windmill|mineshaft|survey_point|cairn)$"]({coords});\n'
        f'  node["tourism"~"^(viewpoint|camp_site|alpine_hut|wilderness_hut|picnic_site)$"]({coords});\n'
        f'  node["historic"~"^(monument|memorial|ruins|castle|fort)$"]({coords});\n'
        f'  way["highway"]({coords});\n'
        f'  way["railway"]({coords});\n'
        f'  way["aeroway"]({coords});\n'
        f'  way["waterway"]({coords});\n'
        f'  way["barrier"~"^(fence|wall|retaining_wall|city_wall)$"]({coords});\n'
        f'  way["natural"~"^(water|wood|forest|scrub|heath|grassland|fell|wetland|mud|bare_rock|scree|shingle|sand|beach|coastline|cliff)$"]({coords});\n'
        f'  way["landuse"~"^(forest|wood|meadow|grass|orchard|vineyard|farmland|recreation_ground|residential|commercial|industrial|retail|military|reservoir|basin)$"]({coords});\n'
        f'  way["leisure"~"^(park|nature_reserve|golf_course|recreation_ground)$"]({coords});\n'
        f'  way["power"]({coords});\n'
        f'  way["man_made"~"^(pipeline|pier|breakwater|dam|weir|bridge|embankment)$"]({coords});\n'
        f'  way["building"]({coords});\n'
        f'  relation["type"="multipolygon"]["boundary"!="administrative"]["natural"~"^(water|wood|forest|scrub|heath|grassland|wetland|mud|bare_rock|sand|beach)$"]({coords});\n'
        f'  relation["type"="multipolygon"]["boundary"!="administrative"]["landuse"~"^(forest|wood|meadow|farmland|recreation_ground|residential|commercial|industrial|retail|military|reservoir|basin)$"]({coords});\n'
        f'  relation["type"="multipolygon"]["boundary"!="administrative"]["leisure"~"^(park|nature_reserve|golf_course|recreation_ground)$"]({coords});\n'
        f'  relation["type"="multipolygon"]["boundary"!="administrative"]["building"]({coords});\n'
        f'  relation["type"="multipolygon"]["boundary"!="administrative"]["aeroway"]({coords});\n'
        f'  relation["type"="multipolygon"]["boundary"!="administrative"]["amenity"~"^(hospital|school|university|place_of_worship|fire_station|police)$"]({coords});\n'
        ");\n"
        "(._;>;);\n"
        "out body;\n"
    )


def extract_overpass_remark(xml_bytes: bytes) -> str | None:
    """Return the Overpass ``<remark>`` error text if present (even on HTTP 200), else ``None``."""
    match = _REMARK_RE.search(xml_bytes)
    if match is None:
        return None
    return match.group(1).decode("utf-8", errors="replace").strip()


def validate_osm_xml_payload(xml_bytes: bytes) -> None:
    """Validate that an Overpass HTTP 200 payload is complete OSM XML with no ``<remark>`` error."""
    remark = extract_overpass_remark(xml_bytes)
    if remark is not None:
        raise RuntimeError(f"Overpass runtime error remark: {remark}")
    head = xml_bytes[:4096]
    tail = xml_bytes[-4096:]
    if b"<osm" not in head or b"</osm>" not in tail:
        raise RuntimeError("Overpass response is truncated or not valid OSM XML")


def validate_osm_xml_file(path: Path) -> None:
    """Validate an on-disk Overpass OSM XML file without buffering the whole file in RAM."""
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("Overpass response is empty")
    with path.open("rb") as fh:
        head = fh.read(4096)
        if b"<osm" not in head:
            raise RuntimeError("Overpass response is truncated or not valid OSM XML")
        if size > 4096:
            fh.seek(max(0, size - 4096))
            tail = fh.read(4096)
        else:
            tail = head
        if b"</osm>" not in tail:
            raise RuntimeError("Overpass response is truncated or not valid OSM XML")

        fh.seek(0)
        overlap = b""
        while True:
            chunk = fh.read(262_144)
            if not chunk:
                break
            window = overlap + chunk
            remark = extract_overpass_remark(window)
            if remark is not None:
                raise RuntimeError(f"Overpass runtime error remark: {remark}")
            overlap = window[-256:]


def sort_osm_xml(input_path: Path, output_path: Path) -> Path:
    """Run ``osmium sort`` when available to guarantee strict nodes -> ways -> relations ID order."""
    osmium_bin = shutil.which("osmium")
    if osmium_bin is None:
        logger.debug("osmium not found on PATH; preserving ID-sorted Overpass XML as-is")
        if input_path.resolve() != output_path.resolve():
            shutil.copy2(input_path, output_path)
        return output_path

    same_file = input_path.resolve() == output_path.resolve()
    target = output_path.with_suffix(".sorted.osm") if same_file else output_path
    cmd = [osmium_bin, "sort", str(input_path), "-o", str(target), "--overwrite"]
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
            f"osmium sort failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    if same_file:
        target.replace(output_path)
    return output_path


class OverpassOSMSource:
    """On-demand OpenStreetMap vector source backed by Overpass API with local disk caching."""

    name: str = "osm-overpass"
    attribution: str = "Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright"

    def __init__(
        self,
        endpoints: Sequence[str] | None = None,
        cache_dir: Path | str | None = None,
        query_timeout_s: int = 60,
        http_timeout_s: float = 75.0,
        rate_limit_backoff_s: float = 30.0,
        transient_backoff_s: float = 2.0,
        max_rounds: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoints = list(endpoints) if endpoints is not None else default_overpass_endpoints()
        if not self.endpoints:
            raise ValueError("At least one Overpass endpoint URL is required")
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir: Path = base_cache / "overpass"
        self.query_timeout_s = query_timeout_s
        self.http_timeout_s = http_timeout_s
        self.rate_limit_backoff_s = rate_limit_backoff_s
        self.transient_backoff_s = transient_backoff_s
        self.max_rounds = max(1, max_rounds)
        self._injected_client = client

    def _cache_path(self, bbox: BBox) -> Path:
        return self.cache_dir / f"overpass_{bbox_cache_key(bbox)[:24]}.osm"

    def _download_with_failover_to_file(
        self,
        query: str,
        dest_path: Path,
        *,
        max_bytes: int = MAX_OVERPASS_RESPONSE_BYTES,
    ) -> Path:
        errors: list[str] = []
        rate_limited_urls: set[str] = set()

        owns_client = self._injected_client is None
        client = (
            self._injected_client
            if self._injected_client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.http_timeout_s, connect=min(10.0, self.http_timeout_s)),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )

        try:
            for round_idx in range(self.max_rounds):
                for url in self.endpoints:
                    if url in rate_limited_urls and self.rate_limit_backoff_s > 0:
                        logger.warning(
                            "Sleeping %.1fs before retrying rate-limited Overpass mirror %s",
                            self.rate_limit_backoff_s,
                            url,
                        )
                        time.sleep(self.rate_limit_backoff_s)

                    logger.info(
                        "Querying Overpass (%s, round %d/%d)",
                        url,
                        round_idx + 1,
                        self.max_rounds,
                    )
                    dest_path.unlink(missing_ok=True)
                    try:
                        with client.stream(
                            "POST",
                            url,
                            data={"data": query},
                            headers={"User-Agent": USER_AGENT},
                        ) as resp:
                            if resp.status_code == 429:
                                rate_limited_urls.add(url)
                                retry_after = resp.headers.get("Retry-After", "")
                                msg = f"{url}: HTTP 429 Too Many Requests (Retry-After={retry_after!r})"
                                logger.warning("Overpass rate-limited; switching mirror: %s", msg)
                                errors.append(msg)
                                if len(self.endpoints) == 1 and self.rate_limit_backoff_s > 0:
                                    delay = self.rate_limit_backoff_s
                                    if retry_after.isdigit():
                                        delay = max(delay, float(retry_after))
                                    time.sleep(delay)
                                continue

                            if resp.status_code in (502, 503, 504):
                                msg = f"{url}: HTTP {resp.status_code} transient server error"
                                logger.warning("Overpass transient error; switching mirror: %s", msg)
                                errors.append(msg)
                                if self.transient_backoff_s > 0:
                                    time.sleep(self.transient_backoff_s)
                                continue

                            if resp.status_code != 200:
                                snippet_chunks: list[bytes] = []
                                snippet_len = 0
                                for chunk in resp.iter_bytes(chunk_size=1024):
                                    if chunk:
                                        snippet_chunks.append(chunk[: max(0, 200 - snippet_len)])
                                        snippet_len += len(chunk)
                                        if snippet_len >= 200:
                                            break
                                snippet = b"".join(snippet_chunks).decode("utf-8", errors="replace").strip()
                                msg = f"{url}: HTTP {resp.status_code} ({snippet})"
                                logger.warning("Overpass unexpected status: %s", msg)
                                errors.append(msg)
                                continue

                            written = 0
                            oversize = False
                            with dest_path.open("wb") as fh:
                                for chunk in resp.iter_bytes(chunk_size=65_536):
                                    if not chunk:
                                        continue
                                    written += len(chunk)
                                    if written > max_bytes:
                                        oversize = True
                                        break
                                    fh.write(chunk)
                            if oversize:
                                dest_path.unlink(missing_ok=True)
                                msg = f"{url}: Overpass response exceeded {max_bytes // (1024 * 1024)} MiB cap"
                                logger.warning("Overpass payload rejected: %s", msg)
                                errors.append(msg)
                                continue
                    except httpx.HTTPError as exc:
                        dest_path.unlink(missing_ok=True)
                        msg = f"{url}: network error ({exc})"
                        logger.warning("Overpass request error: %s", msg)
                        errors.append(msg)
                        if self.transient_backoff_s > 0:
                            time.sleep(self.transient_backoff_s)
                        continue

                    try:
                        validate_osm_xml_file(dest_path)
                    except RuntimeError as exc:
                        dest_path.unlink(missing_ok=True)
                        msg = f"{url}: {exc}"
                        logger.warning("Overpass payload validation failed: %s", msg)
                        errors.append(msg)
                        if self.transient_backoff_s > 0:
                            time.sleep(self.transient_backoff_s)
                        continue

                    return dest_path
        finally:
            if owns_client:
                client.close()

        joined = "; ".join(errors) if errors else "no endpoints attempted"
        raise RuntimeError(f"All Overpass endpoints failed: {joined}")

    def fetch_osm(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch ID-sorted OSM XML covering ``layout.wgs84_fetch_bbox`` (with disk cache)."""
        work_dir.mkdir(parents=True, exist_ok=True)
        query_bbox = round_enclosing_bbox(layout.wgs84_fetch_bbox, step_deg=0.001)
        out_osm = work_dir / "osm_raw.osm"
        cache_path = self._cache_path(query_bbox)

        if cache_path.is_file() and cache_path.stat().st_size > 0:
            try:
                validate_osm_xml_file(cache_path)
            except RuntimeError:
                logger.warning("Ignoring corrupt cached Overpass file: %s", cache_path)
            else:
                logger.info("Using cached Overpass OSM XML: %s", cache_path)
                shutil.copy2(cache_path, out_osm)
                return out_osm

        query = build_overpass_query(query_bbox, timeout_s=self.query_timeout_s)
        raw_tmp = work_dir / "osm_overpass_unsorted.osm"
        self._download_with_failover_to_file(query, raw_tmp)
        sort_osm_xml(raw_tmp, out_osm)
        if raw_tmp.exists():
            raw_tmp.unlink()

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_cache = cache_path.with_suffix(".osm.tmp")
        shutil.copy2(out_osm, tmp_cache)
        tmp_cache.replace(cache_path)

        return out_osm
