"""Line-delimited JSON-RPC 2.0 stdio transport for ``gridforge mcp``.

Strictly zero ``qgis`` / ``PyQt5`` imports. Writes ONLY JSON-RPC frames to ``stdout``
and routes all diagnostic logging strictly to ``stderr``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, TextIO

from gridforge.mcp.protocol import PARSE_ERROR, jsonrpc_error
from gridforge.mcp.server import GridForgeMCPServer

logger = logging.getLogger(__name__)


def _configure_stderr_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


async def _serve_stdio_loop(
    server: GridForgeMCPServer,
    input_stream: TextIO,
    output_stream: TextIO,
) -> None:
    while True:
        raw_line = await asyncio.to_thread(input_stream.readline)
        if raw_line == "":
            break
        line = raw_line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            err_frame = jsonrpc_error(None, PARSE_ERROR, f"Invalid JSON: {exc}")
            output_stream.write(json.dumps(err_frame, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()
            continue

        response = await server.handle_payload(payload)
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()


def run_stdio_server(
    *,
    url: str | None = None,
    api_key: str | None = None,
    public_url: str | None = None,
    data_dir: Path | str | None = None,
    packs_dir: Path | str | None = None,
    app_html_path: Path | str | None = None,
    verbose: bool = False,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    app: Any | None = None,
) -> None:
    """Run the GridForge MCP server over line-delimited JSON-RPC 2.0 ``stdin``/``stdout``."""
    _configure_stderr_logging(verbose=verbose)

    resolved_url = (url or "").strip() or None
    if resolved_url is not None:
        server = GridForgeMCPServer(
            base_url=resolved_url,
            api_key=api_key,
            public_url=public_url or resolved_url,
            app_html_path=app_html_path,
        )
    else:
        target_app = app
        if target_app is None:
            from gridforge.web.app import create_app

            target_app = create_app(data_dir=data_dir, packs_dir=packs_dir)
        server = GridForgeMCPServer(
            app=target_app,
            api_key=api_key,
            public_url=public_url,
            app_html_path=app_html_path,
        )

    in_io = input_stream if input_stream is not None else sys.stdin
    out_io = output_stream if output_stream is not None else sys.stdout
    asyncio.run(_serve_stdio_loop(server, in_io, out_io))
