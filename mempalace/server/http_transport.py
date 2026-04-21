"""
DEPRECATED: HTTP transport shim — redirects to canonical streamable-http path.

Canonical HTTP path: factory.create_server() + mcp.run(transport="streamable-http").
The custom Starlette+Uvicorn implementation previously in this file is removed.
It was NOT FastMCP's streamable-http protocol — it was a POST-only JSON wrapper.

This file is kept only for backward compatibility (fastmcp_server.py re-exports from here).
New code must NOT use serve_http — use factory.create_server() directly.

DEPRECATEDsince: 2026-04-21
"""
from __future__ import annotations

import logging
import warnings

logger = logging.getLogger("mempalace_mcp")


def serve_http(host: str = "127.0.0.1", port: int = 8765, server=None):
    """DEPRECATED: Use factory.create_server() + mcp.run(transport='streamable-http').

    This function now redirects to the canonical FastMCP streamable-http transport.

    Legacy behavior: the removed Starlette+Uvicorn wrapper did NOT implement the
    MCP streamable-http protocol (SSE responses, proper JSON-RPC framing).
    It was a custom POST-only /mcp handler that bypassed FastMCP's HTTP transport.

    Current behavior: calls create_server(shared_server_mode=True) and runs
    with FastMCP's own streamable-http transport — the correct multi-session path.

    Parameters
    ----------
    host : str
        Bind host (default 127.0.0.1).
    port : int
        Bind port (default 8765).
    server : FastMCP
        Ignored. Provided only for backward-compatible signature compatibility.
        The server is always created fresh via create_server(shared_server_mode=True).

    Removed
    -------
    The Starlette+Uvicorn implementation previously in this file is gone.
    It was a separate (non-canonical) HTTP path incompatible with FastMCP's
    streamable-http protocol used by Claude Code's MCP client.
    """
    warnings.warn(
        "serve_http from mempalace.server.http_transport is deprecated. "
        "Use mempalace.server.factory.create_server() + "
        "mcp.run(transport='streamable-http') instead. "
        "The legacy Starlette wrapper in this module was NOT streamable-http — "
        "it was a custom POST-only handler incompatible with Claude Code MCP clients.",
        DeprecationWarning,
        stacklevel=2,
    )
    if server is not None:
        # Backward compat: ignore the passed server, create canonical one.
        # (The old Starlette impl would use the passed server; this is intentional
        # divergence to steer callers toward canonical path.)
        pass

    from .factory import create_server
    _server = create_server(shared_server_mode=True)
    _server.run(transport="streamable-http", host=host, port=port)
