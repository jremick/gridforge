"""Browser access policy, proxy scheme detection, origin-proxy auth, and client IP extraction."""

from __future__ import annotations

import hmac
import ipaddress
import os

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})
DEFAULT_ORIGIN_PROXY_HEADER = "x-gridforge-origin-secret"
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def trust_cf_connecting_ip() -> bool:
    """Return True when ``GRIDFORGE_TRUST_CF_CONNECTING_IP`` is truthy."""
    return os.environ.get("GRIDFORGE_TRUST_CF_CONNECTING_IP", "").strip().lower() in _TRUTHY


def origin_proxy_secret() -> str | None:
    """Return configured ``GRIDFORGE_ORIGIN_PROXY_SECRET`` or None if unset."""
    raw = os.environ.get("GRIDFORGE_ORIGIN_PROXY_SECRET", "").strip()
    return raw or None


def origin_proxy_header_name() -> str:
    """Return normalized header name checked by ``OriginProxyAuthMiddleware``."""
    raw = os.environ.get("GRIDFORGE_ORIGIN_PROXY_HEADER", DEFAULT_ORIGIN_PROXY_HEADER).strip().lower()
    return raw or DEFAULT_ORIGIN_PROXY_HEADER


def has_valid_origin_proxy_secret(headers: Headers) -> bool:
    """Verify the shared edge-proxy secret header using constant-time comparison."""
    expected = origin_proxy_secret()
    if expected is None:
        return True
    supplied = (headers.get(origin_proxy_header_name()) or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _trust_forwarded_proto(scope: Scope | None = None, headers: Headers | None = None) -> bool:
    """Return True when proxy-supplied ``X-Forwarded-Proto`` should be trusted."""
    hdrs = headers if headers is not None else (Headers(scope=scope) if scope is not None else None)
    if trust_cf_connecting_ip():
        if origin_proxy_secret() is not None:
            return hdrs is not None and has_valid_origin_proxy_secret(hdrs)
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
    hdrs = headers if headers is not None else Headers(scope=scope)
    if _trust_forwarded_proto(scope, hdrs):
        xfp = hdrs.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        if xfp in {"http", "https"}:
            return xfp
    return scheme


def is_secure_request(request: Request) -> bool:
    """Return True if the request arrived over HTTPS (directly or via trusted proxy)."""
    return get_effective_scheme(request.scope, request.headers) == "https"


def get_client_ip(request: Request) -> str:
    """Extract client IP, trusting validated ``CF-Connecting-IP`` only when authorized."""
    if trust_cf_connecting_ip() and has_valid_origin_proxy_secret(request.headers):
        raw_cf = request.headers.get("cf-connecting-ip", "").strip()
        if raw_cf:
            try:
                return str(ipaddress.ip_address(raw_cf))
            except ValueError:
                pass
    if request.client and request.client.host:
        return str(request.client.host)
    return "unknown"


class OriginProxyAuthMiddleware:
    """Require a shared edge-proxy secret header when ``GRIDFORGE_ORIGIN_PROXY_SECRET`` is set.

    Blocks direct-to-origin bypasses (e.g. hitting a Railway origin directly instead
    of routing through Cloudflare), while allowing internal healthcheck probes to
    ``/api/health`` from loopback or ``healthcheck.railway.app``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and origin_proxy_secret() is not None:
            headers = Headers(scope=scope)
            path = str(scope.get("path") or "")
            host = headers.get("host", "").split(":", 1)[0].strip().lower()
            client = scope.get("client")
            peer = str(client[0]).strip().lower() if isinstance(client, (tuple, list)) and client else ""

            is_internal_health = path == "/api/health" and (
                host == "healthcheck.railway.app" or peer in _LOOPBACK_HOSTS
            )
            if not is_internal_health and not has_valid_origin_proxy_secret(headers):
                response = PlainTextResponse("Direct origin access is disabled", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
