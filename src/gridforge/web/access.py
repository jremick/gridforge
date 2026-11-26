"""Browser access policy for the single-user GridForge Studio."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class SameOriginWritesMiddleware:
    """Reject cross-origin browser writes, including ordinary multipart form posts."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            scheme = scope["scheme"]
            default_port = ":443" if scheme == "https" else ":80"
            host = headers.get("host", "").lower().removesuffix(default_port)
            origin_mismatch = (
                origin is not None
                and origin.lower().removesuffix(default_port) != f"{scheme}://{host}"
            )
            if origin_mismatch or headers.get("sec-fetch-site") in {"same-site", "cross-site"}:
                response = PlainTextResponse("Cross-origin writes are disabled", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
