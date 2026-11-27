"""Zero-dependency Model Context Protocol (MCP) JSON-RPC 2.0 primitives & SEP-1865 helpers."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

JSONRPC_VERSION: str = "2.0"

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION: str = "2025-06-18"
MCP_APP_EXTENSION_KEY: str = "io.modelcontextprotocol/ui"
MCP_APP_MIME_TYPE: str = "text/html;profile=mcp-app"

# Standard JSON-RPC 2.0 error codes
PARSE_ERROR: int = -32700
INVALID_REQUEST: int = -32600
METHOD_NOT_FOUND: int = -32601
INVALID_PARAMS: int = -32602
INTERNAL_ERROR: int = -32603


class MCPError(Exception):
    """Structured JSON-RPC 2.0 protocol error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error_object(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


def negotiate_protocol_version(requested: str | None) -> str:
    """Negotiate an MCP protocol version, defaulting cleanly to ``DEFAULT_PROTOCOL_VERSION``."""
    if isinstance(requested, str):
        cleaned = requested.strip()
        if cleaned in SUPPORTED_PROTOCOL_VERSIONS:
            return cleaned
    return DEFAULT_PROTOCOL_VERSION


def build_initialize_result(
    *,
    server_name: str,
    server_version: str,
    requested_protocol_version: str | None = None,
    instructions: str | None = None,
    supports_ui_apps: bool = True,
) -> dict[str, Any]:
    """Build a compliant MCP ``initialize`` response payload with MCP Apps extension awareness."""
    protocol_version = negotiate_protocol_version(requested_protocol_version)
    capabilities: dict[str, Any] = {
        "tools": {},
        "resources": {},
        "prompts": {},
    }
    if supports_ui_apps:
        capabilities["extensions"] = {
            MCP_APP_EXTENSION_KEY: {
                "mimeTypes": [MCP_APP_MIME_TYPE],
            }
        }
    result: dict[str, Any] = {
        "protocolVersion": protocol_version,
        "capabilities": capabilities,
        "serverInfo": {
            "name": server_name,
            "version": server_version,
        },
    }
    if instructions:
        result["instructions"] = instructions
    return result


@dataclass(frozen=True)
class MCPToolDefinition:
    """MCP ``tools/list`` tool descriptor with optional annotations and SEP-1865 ``_meta``."""

    name: str
    description: str
    input_schema: dict[str, Any]
    title: str | None = None
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title is not None:
            out["title"] = self.title
        if self.annotations is not None:
            out["annotations"] = dict(self.annotations)
        if self.meta is not None:
            out["_meta"] = dict(self.meta)
        return out


@dataclass(frozen=True)
class MCPResourceDefinition:
    """MCP ``resources/list`` resource descriptor with optional SEP-1865 ``_meta``."""

    uri: str
    name: str
    description: str
    mime_type: str
    title: str | None = None
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }
        if self.title is not None:
            out["title"] = self.title
        if self.meta is not None:
            out["_meta"] = dict(self.meta)
        return out


@dataclass(frozen=True)
class MCPPromptArgument:
    """Single prompt argument descriptor for ``prompts/list``."""

    name: str
    description: str
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
        }


@dataclass(frozen=True)
class MCPPromptDefinition:
    """MCP ``prompts/list`` prompt descriptor."""

    name: str
    description: str
    title: str | None = None
    arguments: tuple[MCPPromptArgument, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
        }
        if self.title is not None:
            out["title"] = self.title
        if self.arguments:
            out["arguments"] = [arg.to_dict() for arg in self.arguments]
        return out


def text_content(text: str) -> dict[str, Any]:
    """Build an MCP ``TextContent`` block."""
    return {"type": "text", "text": text}


def image_content(image_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
    """Build an MCP ``ImageContent`` block with base64-encoded payload."""
    return {
        "type": "image",
        "mimeType": mime_type,
        "data": base64.b64encode(image_bytes).decode("ascii"),
    }


def resource_link_content(
    uri: str,
    name: str,
    *,
    description: str | None = None,
    mime_type: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Build an MCP ``ResourceLink`` content item (MCP 2025-06-18)."""
    item: dict[str, Any] = {
        "type": "resource_link",
        "uri": uri,
        "name": name,
    }
    if title is not None:
        item["title"] = title
    if description is not None:
        item["description"] = description
    if mime_type is not None:
        item["mimeType"] = mime_type
    return item


def call_tool_result(
    content: list[dict[str, Any]],
    *,
    structured_content: dict[str, Any] | None = None,
    is_error: bool = False,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an MCP ``CallToolResult`` with both ``content`` and optional ``structuredContent``."""
    out: dict[str, Any] = {
        "content": list(content),
        "isError": bool(is_error),
    }
    if structured_content is not None:
        out["structuredContent"] = structured_content
    if meta is not None:
        out["_meta"] = dict(meta)
    return out


def resource_read_result(
    uri: str,
    *,
    mime_type: str,
    text: str | None = None,
    blob_bytes: bytes | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an MCP ``resources/read`` result envelope preserving ``_meta`` on content items."""
    item: dict[str, Any] = {
        "uri": uri,
        "mimeType": mime_type,
    }
    if text is not None:
        item["text"] = text
    elif blob_bytes is not None:
        item["blob"] = base64.b64encode(blob_bytes).decode("ascii")
    else:
        item["text"] = ""
    if meta is not None:
        item["_meta"] = dict(meta)
    return {"contents": [item]}


def jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response object."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "result": result,
    }


def jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response object."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "error": err,
    }
