"""MCP Streamable HTTP router (``POST/GET/DELETE /mcp``) for FastAPI.

Strictly zero ``qgis`` / ``PyQt5`` imports. Handles ``Mcp-Session-Id``,
``MCP-Protocol-Version``, single & batch JSON-RPC 2.0 payloads, ``202 Accepted``
notifications, auth header forwarding into in-process ASGI calls, and public URL inference.
"""

from __future__ import annotations

import os
import secrets
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from gridforge.mcp.protocol import (
    DEFAULT_PROTOCOL_VERSION,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    jsonrpc_error,
    negotiate_protocol_version,
)
from gridforge.mcp.server import GridForgeMCPServer
from gridforge.web.access import get_effective_scheme, origin_proxy_header_name
from gridforge.web.auth import resolve_auth_mode

_MAX_TRACKED_SESSIONS: int = 1024


def _extract_supplied_api_key(request: Request) -> str | None:
    auth_hdr = (request.headers.get("authorization") or "").strip()
    if auth_hdr.lower().startswith("bearer "):
        tok = auth_hdr[7:].strip()
        if tok:
            return tok
    x_key = (request.headers.get("x-api-key") or "").strip()
    return x_key or None


def _check_single_tenant_api_key(request: Request, target_app: Any) -> JSONResponse | None:
    """When ``auth_mode == 'none'`` and ``GRIDFORGE_API_KEY`` is set, require constant-time key match."""
    auth_cfg = getattr(getattr(target_app, "state", None), "auth_config", None)
    auth_mode = getattr(auth_cfg, "auth_mode", None) or resolve_auth_mode()
    if str(auth_mode).strip().lower() != "none":
        return None
    expected_key = os.environ.get("GRIDFORGE_API_KEY", "").strip()
    if not expected_key:
        return None
    supplied = _extract_supplied_api_key(request)
    if supplied is None or not secrets.compare_digest(supplied, expected_key):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or missing API key."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def _infer_public_url(request: Request, explicit_public_url: str | None) -> str | None:
    if explicit_public_url and explicit_public_url.strip():
        return explicit_public_url.strip().rstrip("/")
    env_pub = os.environ.get("GRIDFORGE_PUBLIC_URL", "").strip()
    if env_pub:
        return env_pub.rstrip("/")
    host = (request.headers.get("host") or "").strip()
    if not host:
        return None
    scheme = get_effective_scheme(request.scope, request.headers)
    return f"{scheme}://{host}"


def _extract_forwarded_auth_headers(request: Request) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    auth_hdr = request.headers.get("authorization")
    if auth_hdr:
        forwarded["Authorization"] = auth_hdr
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        forwarded["X-API-Key"] = x_api_key
    proxy_hdr_name = origin_proxy_header_name()
    proxy_hdr_val = request.headers.get(proxy_hdr_name)
    if proxy_hdr_val:
        forwarded[proxy_hdr_name] = proxy_hdr_val
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        forwarded["CF-Connecting-IP"] = cf_ip
    xfp = request.headers.get("x-forwarded-proto")
    if xfp:
        forwarded["X-Forwarded-Proto"] = xfp
    return forwarded


def create_mcp_router(
    get_app: Callable[[], Any],
    *,
    public_url: str | None = None,
    app_html_path: Path | str | None = None,
) -> APIRouter:
    """Create a FastAPI ``APIRouter`` implementing MCP Streamable HTTP on ``/mcp``."""
    router = APIRouter(tags=["mcp"])
    server = GridForgeMCPServer(
        get_app=get_app,
        public_url=public_url,
        app_html_path=app_html_path,
    )

    sessions: OrderedDict[str, str] = OrderedDict()
    closed_sessions: OrderedDict[str, None] = OrderedDict()

    def _remember_session(session_id: str, proto_version: str) -> None:
        sessions[session_id] = proto_version
        sessions.move_to_end(session_id)
        while len(sessions) > _MAX_TRACKED_SESSIONS:
            sessions.popitem(last=False)

    def _mark_closed(session_id: str) -> None:
        sessions.pop(session_id, None)
        closed_sessions[session_id] = None
        closed_sessions.move_to_end(session_id)
        while len(closed_sessions) > _MAX_TRACKED_SESSIONS:
            closed_sessions.popitem(last=False)

    @router.post("/mcp", include_in_schema=False)
    async def mcp_post(request: Request) -> Response:
        target_app = get_app()
        auth_err = _check_single_tenant_api_key(request, target_app)
        if auth_err is not None:
            return auth_err

        incoming_session_id = (request.headers.get("mcp-session-id") or "").strip() or None
        if incoming_session_id and incoming_session_id in closed_sessions:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "MCP session has been terminated."},
            )

        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=jsonrpc_error(None, PARSE_ERROR, "Invalid JSON payload."),
            )

        forwarded_headers = _extract_forwarded_auth_headers(request)
        eff_public_url = _infer_public_url(request, public_url)
        client_addr = (
            (str(request.client.host), int(request.client.port))
            if request.client is not None and request.client.host
            else None
        )

        result_payload = await server.handle_payload(
            payload,
            request_headers=forwarded_headers,
            public_url=eff_public_url,
            client_addr=client_addr,
        )

        # Resolve protocol version & session lifecycle
        req_proto_hdr = request.headers.get("mcp-protocol-version")
        negotiated_version = negotiate_protocol_version(req_proto_hdr)
        active_session_id = incoming_session_id

        # Inspect initialize result(s) to mint a new session ID & record negotiated version
        init_results: list[dict[str, Any]] = []
        if isinstance(payload, dict) and payload.get("method") == "initialize" and isinstance(result_payload, dict):
            init_results.append(result_payload)
        elif isinstance(payload, list) and isinstance(result_payload, list):
            for req_item, resp_item in zip(payload, result_payload):
                if isinstance(req_item, dict) and req_item.get("method") == "initialize" and isinstance(resp_item, dict):
                    init_results.append(resp_item)

        for init_resp in init_results:
            res_obj = init_resp.get("result")
            if isinstance(res_obj, dict) and isinstance(res_obj.get("protocolVersion"), str):
                negotiated_version = res_obj["protocolVersion"]
                active_session_id = active_session_id or secrets.token_urlsafe(24)
                _remember_session(active_session_id, negotiated_version)

        if active_session_id and active_session_id in sessions:
            negotiated_version = sessions[active_session_id]
            sessions.move_to_end(active_session_id)

        resp_headers: dict[str, str] = {
            "MCP-Protocol-Version": negotiated_version or DEFAULT_PROTOCOL_VERSION,
            "Cache-Control": "no-store",
        }
        if active_session_id:
            resp_headers["Mcp-Session-Id"] = active_session_id

        if result_payload is None:
            return Response(status_code=status.HTTP_202_ACCEPTED, headers=resp_headers)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result_payload,
            headers=resp_headers,
        )

    @router.get("/mcp", include_in_schema=False)
    async def mcp_get(request: Request) -> Response:
        target_app = get_app()
        auth_err = _check_single_tenant_api_key(request, target_app)
        if auth_err is not None:
            return auth_err

        accept = (request.headers.get("accept") or "").lower()
        if "text/event-stream" in accept:
            # Per MCP Streamable HTTP spec: 405 indicates no server-initiated SSE stream
            return Response(
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                headers={"Allow": "POST, DELETE"},
            )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "service": "gridforge-mcp",
                "transport": "streamable-http",
                "endpoint": "/mcp",
                "protocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            },
            headers={"MCP-Protocol-Version": DEFAULT_PROTOCOL_VERSION},
        )

    @router.delete("/mcp", include_in_schema=False)
    async def mcp_delete(request: Request) -> Response:
        target_app = get_app()
        auth_err = _check_single_tenant_api_key(request, target_app)
        if auth_err is not None:
            return auth_err

        session_id = (request.headers.get("mcp-session-id") or "").strip()
        if not session_id:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Missing Mcp-Session-Id header."},
            )
        if session_id not in sessions:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "Unknown or already closed MCP session."},
            )
        _mark_closed(session_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
