"""Bounded reads for provider responses and their on-disk caches."""

from __future__ import annotations

import ipaddress
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx


def _validate_redirect_url(
    origin: httpx.URL,
    target: httpx.URL,
    *,
    allowed_host_suffixes: tuple[str, ...] | None = None,
) -> None:
    """Block redirect downgrades, private/loopback targets, and unapproved cross-host redirects."""
    origin_scheme = origin.scheme.lower()
    target_scheme = target.scheme.lower()
    if target_scheme != "https" and not (origin_scheme == "http" and target_scheme == "http"):
        raise ValueError("Provider redirect must use HTTPS")
    if target.username or target.password:
        raise ValueError("Provider redirect must not include credentials")

    origin_host = (origin.host or "").lower().rstrip(".")
    target_host = (target.host or "").lower().rstrip(".")
    if not target_host or target_host == "localhost" or target_host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ValueError("Provider redirect points to a disallowed local host")
    try:
        ip = ipaddress.ip_address(target_host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ValueError("Provider redirect points to a non-global IP address")

    if target_host == origin_host:
        return
    if allowed_host_suffixes and any(
        target_host == suffix.lower() or target_host.endswith(f".{suffix.lower()}")
        for suffix in allowed_host_suffixes
    ):
        return
    raise ValueError("Provider redirect leaves the approved host boundary")


@contextmanager
def stream_provider_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None,
    user_agent: str,
    deadline: float,
    allowed_redirect_host_suffixes: tuple[str, ...] | None = None,
) -> Iterator[httpx.Response]:
    """Skip redirect bodies and validate redirect hops within the caller's deadline."""
    request = client.build_request(
        "GET", url, params=params, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
    )
    origin_url = request.url
    for _ in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Provider request exceeds the time budget")
        request.extensions["timeout"] = httpx.Timeout(
            remaining, connect=min(8.0, remaining)
        ).as_dict()
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            if response.next_request is None:
                yield response
                return
            _validate_redirect_url(
                origin_url,
                response.next_request.url,
                allowed_host_suffixes=allowed_redirect_host_suffixes,
            )
            request = response.next_request
        finally:
            response.close()
    raise ValueError("Provider request exceeds the redirect budget")


def bounded_response_chunks(
    response: httpx.Response, *, max_bytes: int, deadline: float
) -> Iterator[bytes]:
    """Yield an identity-encoded body within a cumulative byte and time budget."""
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower() or "identity"
    if encoding != "identity":
        raise ValueError("Provider response must use identity content encoding")

    length = response.headers.get("Content-Length")
    if length is not None and not 0 <= int(length) <= max_bytes:
        raise ValueError("Provider response exceeds the byte budget")

    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("Provider response exceeds the byte budget")
        if time.monotonic() > deadline:
            raise ValueError("Provider response exceeds the time budget")
        yield chunk

    if length is not None and total != int(length):
        raise ValueError("Provider response size does not match Content-Length")


def read_bounded_file(path: Path, max_bytes: int) -> bytes:
    """Read at most the byte budget, including when the file changes during a read."""
    with path.open("rb") as cached:
        if cached.seek(0, 2) > max_bytes:
            raise ValueError("Cached file exceeds the byte budget")
        cached.seek(0)
        content = cached.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("Cached file exceeds the byte budget")
    return content


def write_cache_bytes(path: Path, content: bytes) -> None:
    """Publish bytes atomically without sharing temporary names across writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as pending:
        pending_path = Path(pending.name)
        try:
            pending.write(content)
            pending.close()
            pending_path.replace(path)
        finally:
            pending_path.unlink(missing_ok=True)
