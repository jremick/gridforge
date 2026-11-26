"""Browser access policy, proxy scheme detection, and client IP extraction."""

from __future__ import annotations

import ipaddress
import os

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})


def trust_cf_connecting_ip() -> bool:
    """Return True when ``GRIDFORGE_TRUST_CF_CONNECTING_IP`` is truthy."""
    return os.environ.get("GRIDFORGE_TRUST_CF_CONNECTING_IP", "").strip().lower() in _TRUTHY


def _trust_forwarded_proto(scope: Scope | None = None) -> bool:
    """Return True when proxy-supplied ``X-Forwarded-Proto`` should be trusted."""
    if trust_cf_connecting_ip():
        return True
    fwd = os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    if not fwd:
        return False
    parts = [part.strip() for part in fwd.split(",") if part.strip()]
    if "*" in parts:
        return True

    client = scope.get("client") if scope is not None else None
    peer_host = str(client[0]).strip() if isinstance(client, (tuple, list)) and client else ""
    if not peer_host:
        return False
    try:
        peer_ip = ipaddress.ip_address(peer_host)
    except ValueError:
        return False

    for entry in parts:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if peer_ip in net:
            return True
    return False


def get_effective_scheme(scope: Scope, headers: Headers | None = None) -> str:
    """Resolve effective request scheme, honoring ``X-Forwarded-Proto`` only when trusted."""
    scheme = str(scope.get("scheme") or "http").lower()
    if _trust_forwarded_proto(scope):
        hdrs = headers if headers is not None else Headers(scope=scope)
        xfp = hdrs.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        if xfp in {"http", "https"}:
            return xfp
    return scheme


def is_secure_request(request: Request) -> bool:
    """Return True if the request arrived over HTTPS (directly or via trusted proxy)."""
    return get_effective_scheme(request.scope, request.headers) == "https"


def get_client_ip(request: Request) -> str:
    """Extract client IP, trusting validated ``CF-Connecting-IP`` only when enabled."""
    if trust_cf_connecting_ip():
        raw_cf = request.headers.get("cf-connecting-ip", "").strip()
        if raw_cf:
            try:
                return str(ipaddress.ip_address(raw_cf))
            except ValueError:
                pass
    if request.client and request.client.host:
        return str(request.client.host)
    return "unknown"


class SameOriginWritesMiddleware:
    """Reject cross-origin browser writes, including ordinary multipart form posts."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            scheme = get_effective_scheme(scope, headers)
            default_port = ":443" if scheme == "https" else ":80"
            host = headers.get("host", "").strip().lower().removesuffix(default_port)
            origin_norm = (
                origin.strip().lower().removesuffix(default_port)
                if origin is not None
                else None
            )
            origin_mismatch = (
                origin_norm is not None and origin_norm != f"{scheme}://{host}"
            )
            sec_fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
            if origin_mismatch or sec_fetch_site in {"same-site", "cross-site"}:
                response = PlainTextResponse("Cross-origin writes are disabled", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
