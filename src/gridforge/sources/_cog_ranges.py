"""Expose approved cloud GeoTIFF byte ranges to GDAL for one operation."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from gridforge.sources._http import bounded_response_chunks

logger = logging.getLogger(__name__)

_MAX_RANGE_BYTES = 32 * 1024 * 1024
_MAX_READ_BYTES = 512 * 1024 * 1024
_MAX_READ_REQUESTS = 4096
_RANGE = re.compile(r"bytes=(\d{1,15})-(\d{1,15})")
_CONTENT_RANGE = re.compile(r"bytes (\d{1,15})-(\d{1,15})/(\d{1,15})")


@contextmanager
def cog_range_paths(
    hrefs: Sequence[str], client: httpx.Client, *, timeout_s: float
) -> Iterator[list[str]]:
    """Serve fixed, already validated asset URLs without passing them to GDAL.

    GDAL 3.10 follows redirects in /vsicurl/. This loopback reader performs
    only a HEAD or a single range GET and returns no redirect targets to GDAL.
    """
    prefix = secrets.token_urlsafe(24)
    paths = {
        f"/{prefix}/{index}.tif": href for index, href in enumerate(hrefs)
    }
    remaining_bytes = _MAX_READ_BYTES
    remaining_requests = _MAX_READ_REQUESTS
    deadline = time.monotonic() + timeout_s
    request_lock = threading.Lock()
    stopped = threading.Event()

    class RangeHandler(BaseHTTPRequestHandler):
        timeout = 10.0

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_HEAD(self) -> None:
            self._serve_asset()

        def do_GET(self) -> None:
            self._serve_asset()

        def _serve_asset(self) -> None:
            href = paths.get(self.path)
            if href is None:
                self.send_error(404)
                return
            # Keep cumulative accounting serial. The accept loop can still
            # shut down immediately if a warp times out during an upstream read.
            with request_lock:
                self._forward(href)

        def _forward(self, href: str) -> None:
            nonlocal remaining_bytes, remaining_requests
            header_sent = False
            try:
                remaining_requests -= 1
                if stopped.is_set() or remaining_requests < 0 or time.monotonic() > deadline:
                    raise ValueError("COG request budget exceeded")
                headers = {"Accept-Encoding": "identity"}
                is_head = self.command == "HEAD"
                if not is_head:
                    requested_range = self.headers.get("Range", "")
                    requested = _RANGE.fullmatch(requested_range)
                    if requested is None:
                        raise ValueError("COG reads require a single finite byte range")
                    start, end = (int(number) for number in requested.groups())
                    if not 0 < end - start + 1 <= min(_MAX_RANGE_BYTES, remaining_bytes):
                        raise ValueError("COG byte budget exceeded")
                    headers["Range"] = requested_range
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("COG request budget exceeded")
                with client.stream(
                    self.command,
                    href,
                    headers=headers,
                    follow_redirects=False,
                    timeout=httpx.Timeout(remaining, connect=min(8.0, remaining)),
                ) as response:
                    length = int(response.headers.get("Content-Length", "0"))
                    encoding = (response.headers.get("Content-Encoding") or "").strip().lower() or "identity"
                    if encoding != "identity":
                        raise ValueError("Unsupported COG content encoding")
                    if is_head:
                        if response.status_code != 200 or length <= 0:
                            raise ValueError("Provider did not supply direct COG metadata")
                    else:
                        content_range = response.headers.get("Content-Range", "")
                        actual = _CONTENT_RANGE.fullmatch(content_range)
                        if response.status_code != 206 or actual is None:
                            raise ValueError("Provider did not supply a direct COG byte range")
                        first, last, size = (int(number) for number in actual.groups())
                        if first != start or last != min(end, size - 1) or length != last - first + 1:
                            raise ValueError("COG range response did not match the request")
                        if length <= 0:
                            raise ValueError("COG range response was empty")
                        remaining_bytes -= length
                    self.send_response(response.status_code)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    if not is_head:
                        self.send_header("Content-Range", content_range)
                    self.end_headers()
                    header_sent = True
                    if not is_head:
                        for chunk in bounded_response_chunks(response, max_bytes=length, deadline=deadline):
                            if stopped.is_set():
                                break
                            self.wfile.write(chunk)
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                logger.debug("Canadian DEM range read declined (%s)", exc)
                if not header_sent and not stopped.is_set():
                    try:
                        self.send_error(502)
                    except OSError:
                        pass

    with ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield [f"/vsicurl/http://127.0.0.1:{port}{path}" for path in paths]
        finally:
            stopped.set()
            server.shutdown()
            thread.join()
