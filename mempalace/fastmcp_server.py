#!/usr/bin/env python3
"""
MemPalace FastMCP Server — backward-compatibility shim.
=======================================================
All tools are now in mempalace/server/. This file re-exports the public API
so existing imports (e.g. from mempalace.fastmcp_server import create_server)
continue to work without changes.

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
