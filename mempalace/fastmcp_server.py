#!/usr/bin/env python3
"""
MemPalace FastMCP Server — canonical entry point.
==================================================
All tools live in mempalace/server/. This file re-exports the public API
so existing imports (e.g. from mempalace.fastmcp_server import create_server)
continue to work without changes.

TRANSPORT CONTRACT
-------------------
Canonical multi-session HTTP path (6× Claude Code parallel sessions):
    mcp = create_server(shared_server_mode=True)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8765)

Stdio path (single-session / dev):
    mcp = create_server()
    mcp.run()   # stdio transport

DEPRECATED serve_http():
    serve_http() is a backward-compat shim that redirects to the canonical
    streamable-http path above. The legacy Starlette+Uvicorn wrapper it once
    contained is removed — it was NOT FastMCP's streamable-http protocol.
    Do NOT use serve_http() in new code.

Install: claude mcp add mempalace -- python -m mempalace.fastmcp_server [--palace /path/to/palace]
"""
from .server.factory import create_server
from .server.http_transport import serve_http
from .server._search_tools import PALACE_PROTOCOL, AAAK_SPEC
from .server._infrastructure import wal_executor, bg_executor, wal_log_async, wal_log, get_wal_path
from .server._infrastructure import wal_executor as _wal_executor
from .server._infrastructure import bg_executor as _bg_executor

# Backward-compat: module-level _status_cache for legacy code/tests that
# access the old module-level cache directly. Production code should use
# server._status_cache (per-server-instance StatusCache).
_status_cache = {"data": None, "ts": 0.0}


def _get_status_cache() -> dict:
    """Return the legacy module-level status cache dict for backward compat."""
    return _status_cache


# Re-export public symbols for backward compatibility
__all__ = [
    "create_server",
    "serve_http",
    "PALACE_PROTOCOL",
    "AAAK_SPEC",
    "wal_executor",
    "bg_executor",
    "wal_log_async",
    "wal_log",
    "get_wal_path",
    "_status_cache",
]
