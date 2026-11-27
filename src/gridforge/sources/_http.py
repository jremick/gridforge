"""Bounded reads for provider responses and their on-disk caches."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx


@contextmanager
def stream_provider_get(
    client: httpx.Client, url: str, *, params: dict[str, str] | None, user_agent: str, deadline: float
) -> Iterator[httpx.Response]:
    """Skip redirect bodies so they cannot bypass a caller's streaming limit."""
    request = client.build_request(
        "GET", url, params=params, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
    )
    for _ in range(4):
        if time.monotonic() > deadline:
            raise ValueError("Provider request exceeds the time budget")
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            if response.next_request is None:
                yield response
                return
            request = response.next_request
        finally:
            response.close()
    raise ValueError("Provider request exceeds the redirect budget")


def bounded_response_chunks(
    response: httpx.Response, *, max_bytes: int, deadline: float
) -> Iterator[bytes]:
    """Yield an identity-encoded body within a cumulative byte and time budget."""
    encoding = response.headers.get("Content-Encoding", "identity").lower()
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
