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

logger = logging.getLogger(__name__)

PRIMARY_OVERPASS_URL: Final[str] = "https://overpass-api.de/api/interpreter"
FALLBACK_OVERPASS_URL: Final[str] = "https://overpass.private.coffee/api/interpreter"
USER_AGENT: Final[str] = "gridforge/0.1 (+https://github.com/gridforge/gridforge)"

_REMARK_RE = re.compile(rb"<remark\b[^>]*>(.*?)</remark>", flags=re.IGNORECASE | re.DOTALL)


def default_overpass_endpoints() -> list[str]:
    """Return deduplicated default Overpass endpoints honoring ``OVERPASS_URL``."""
    raw = [
        os.environ.get("OVERPASS_URL", PRIMARY_OVERPASS_URL).strip() or PRIMARY_OVERPASS_URL,
        FALLBACK_OVERPASS_URL,
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
    payload = f"{rounded.min_x:.3f},{rounded.min_y:.3f},{rounded.max_x:.3f},{rounded.max_y:.3f}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def build_overpass_query(
    bbox: BBox,
    timeout_s: int = 90,
    maxsize_bytes: int = 536870912,
) -> str:
    """Build a GDAL-compatible Overpass QL query using a single union and ID-sorted ``out body;``.

    Never uses ``out skel qt;`` or split ``out`` blocks, which violate GDAL's strict
    ``nodes -> ways -> relations`` ascending-ID ordering requirement.
    """
    s, w, n, e = bbox.min_y, bbox.min_x, bbox.max_y, bbox.max_x
    return (
        f"[out:xml][timeout:{timeout_s}][maxsize:{maxsize_bytes}];\n"
        f"(nwr({s:.6f},{w:.6f},{n:.6f},{e:.6f}); >;);\n"
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
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
        query_timeout_s: int = 90,
        http_timeout_s: float = 120.0,
        rate_limit_backoff_s: float = 30.0,
        transient_backoff_s: float = 3.0,
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

    def _download_with_failover(self, query: str) -> bytes:
        errors: list[str] = []
        rate_limited_urls: set[str] = set()

        owns_client = self._injected_client is None
        client = (
            self._injected_client
            if self._injected_client is not None
            else httpx.Client(
                timeout=self.http_timeout_s,
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
                    try:
                        resp = client.post(
                            url,
                            data={"data": query},
                            headers={"User-Agent": USER_AGENT},
                        )
                    except httpx.HTTPError as exc:
                        msg = f"{url}: network error ({exc})"
                        logger.warning("Overpass request error: %s", msg)
                        errors.append(msg)
                        if self.transient_backoff_s > 0:
                            time.sleep(self.transient_backoff_s)
                        continue

                    if resp.status_code == 429:
                        rate_limited_urls.add(url)
                        retry_after = resp.headers.get("Retry-After", "")
                        msg = f"{url}: HTTP 429 Too Many Requests (Retry-After={retry_after!r})"
                        logger.warning("Overpass rate-limited; switching mirror: %s", msg)
                        errors.append(msg)
                        # If there is only a single endpoint, back off immediately before retrying it
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
                        msg = f"{url}: HTTP {resp.status_code} ({resp.text[:200].strip()})"
                        logger.warning("Overpass unexpected status: %s", msg)
                        errors.append(msg)
                        continue

                    payload = resp.content
                    try:
                        validate_osm_xml_payload(payload)
                    except RuntimeError as exc:
                        msg = f"{url}: {exc}"
                        logger.warning("Overpass payload validation failed: %s", msg)
                        errors.append(msg)
                        if self.transient_backoff_s > 0:
                            time.sleep(self.transient_backoff_s)
                        continue

                    return payload
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
            cached_bytes = cache_path.read_bytes()
            try:
                validate_osm_xml_payload(cached_bytes)
            except RuntimeError:
                logger.warning("Ignoring corrupt cached Overpass file: %s", cache_path)
            else:
                logger.info("Using cached Overpass OSM XML: %s", cache_path)
                shutil.copy2(cache_path, out_osm)
                return out_osm

        query = build_overpass_query(query_bbox, timeout_s=self.query_timeout_s)
        payload = self._download_with_failover(query)

        raw_tmp = work_dir / "osm_overpass_unsorted.osm"
        raw_tmp.write_bytes(payload)
        sort_osm_xml(raw_tmp, out_osm)
        if raw_tmp.exists():
            raw_tmp.unlink()

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_cache = cache_path.with_suffix(".osm.tmp")
        shutil.copy2(out_osm, tmp_cache)
        tmp_cache.replace(cache_path)

        return out_osm
