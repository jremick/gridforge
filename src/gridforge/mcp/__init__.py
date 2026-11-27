"""Native GridForge Model Context Protocol (MCP) server, SEP-1865 MCP Apps, and transports.

This package is strictly free of ``qgis`` and ``PyQt5`` imports so it can run safely inside
lightweight API/web processes and standalone stdio CLI sessions.
"""

from __future__ import annotations

from gridforge.mcp.http import create_mcp_router
from gridforge.mcp.protocol import (
    DEFAULT_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    MCPError,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPResourceDefinition,
    MCPToolDefinition,
    call_tool_result,
    image_content,
    negotiate_protocol_version,
    resource_link_content,
    resource_read_result,
    text_content,
)
from gridforge.mcp.server import (
    APP_MIME_TYPE,
    APP_ONLY,
    APP_URI,
    INLINE_UI,
    STUDIO_UI,
    GridForgeMCPServer,
)
from gridforge.mcp.stdio import run_stdio_server

__all__ = [
    "APP_MIME_TYPE",
    "APP_ONLY",
    "APP_URI",
    "DEFAULT_PROTOCOL_VERSION",
    "GridForgeMCPServer",
    "INLINE_UI",
    "MCPError",
    "MCPPromptArgument",
    "MCPPromptDefinition",
    "MCPResourceDefinition",
    "MCPToolDefinition",
    "STUDIO_UI",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "call_tool_result",
    "create_mcp_router",
    "image_content",
    "negotiate_protocol_version",
    "resource_link_content",
    "resource_read_result",
    "run_stdio_server",
    "text_content",
]
