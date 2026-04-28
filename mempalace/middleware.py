"""
MemPalace FastMCP Middleware Stack
==================================
FastMCP v2.9 built-in error handling + custom caching with MemPalace invalidation.

NOTE on caching: FastMCP's built-in ResponseCachingMiddleware uses AsyncKeyValue
backends that do NOT expose an invalidate() method. MemPalace keeps its custom
TTL cache (which has invalidate()) and merges CacheInvalidationMiddleware logic
into MemPalaceCachingMiddleware.on_call_tool to avoid a separate middleware class.
"""

import asyncio
import logging
import threading
import time
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware

from .settings import settings

logger = logging.getLogger("mempalace_mcp")


# ─────────────────────────────────────────────────────────────────────────────
# WRITE TOOLS THAT TRIGGER CACHE INVALIDATION
# ─────────────────────────────────────────────────────────────────────────────

_WRITE_TOOLS = {
    "mempalace_add_drawer",
    "mempalace_remember_code",
    "mempalace_consolidate",
    "mempalace_diary_write",
    "mempalace_delete_drawer",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
}


def _should_invalidate(tool_name: str, result: Any) -> bool:
    """Return True when a write tool succeeded and cache should be invalidated."""
    if tool_name not in _WRITE_TOOLS:
        return False
    if tool_name in (
        "mempalace_add_drawer",
        "mempalace_remember_code",
        "mempalace_diary_write",
        "mempalace_delete_drawer",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
    ):
        return isinstance(result, dict) and result.get("success", False)
    if tool_name == "mempalace_consolidate":
        if isinstance(result, dict):
            return result.get("success", False) or result.get("merged", 0) > 0
    return False


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE CACHING MIDDLEWARE (custom TTL cache with invalidate)
# ─────────────────────────────────────────────────────────────────────────────
# NOTE: FastMCP's built-in ResponseCachingMiddleware uses AsyncKeyValue stores
# that do NOT expose invalidate(). MemPalace needs invalidate() for write-aware
# cache invalidation, so we keep the custom implementation from the original code
# (which was already correct and well-tested). The CacheInvalidationMiddleware
# logic is merged into MemPalaceCachingMiddleware.on_call_tool below.

_MISSING = object()


class MemPalaceCachingMiddleware(Middleware):
    """
    TTL-based response cache for metadata read operations with write-aware
    cache invalidation merged in (no separate CacheInvalidationMiddleware).

    Caches responses for:
    - mempalace_status (TTL 5s)
    - mempalace_list_wings (TTL 30s)
    - mempalace_list_rooms (TTL 30s)
    - mempalace_get_taxonomy (TTL 30s)

    Cache is keyed by tool name + sorted request params.

    On write success (for the 7 write tools), cache is invalidated so the next
    read gets fresh data.
    """

    TOOL_TTL = {
        "mempalace_status": float(settings.cache_ttl_status),
        "mempalace_list_wings": float(settings.cache_ttl_metadata),
        "mempalace_list_rooms": float(settings.cache_ttl_metadata),
        "mempalace_get_taxonomy": float(settings.cache_ttl_metadata),
    }

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Any, float]] = {}
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    def _make_key(self, method: str, params: dict) -> str:
        """Build cache key from tool name and params."""
        if params:
            sorted_params = sorted(params.items())
            param_str = "&".join(f"{k}={v}" for k, v in sorted_params if k != "ctx")
            return f"{method}:{param_str}"
        return method

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        tool_name = context.message.get("name") if isinstance(context.message, dict) else None

        # Fast path — check cache WITHOUT lock for cached reads
        if tool_name in self.TOOL_TTL:
            cache_key = self._make_key(tool_name, context.message if isinstance(context.message, dict) else {})
            if cache_key in self._cache:
                data, ts = self._cache[cache_key]
                if time.monotonic() - ts < self.TOOL_TTL.get(tool_name, 30.0):
                    logger.debug(f"cache hit: {tool_name}")
                    return data
                try:
                    del self._cache[cache_key]
                except KeyError:
                    pass

        # All tools: call the handler
        result = await call_next(context)

        # Cached reads: store result under lock
        if tool_name in self.TOOL_TTL and isinstance(result, dict) and "error" not in result:
            async with self._async_lock:
                if cache_key not in self._cache:
                    self._cache[cache_key] = (result, time.monotonic())

        # Write-aware invalidation: runs for ALL tools (cached or not)
        if tool_name and _should_invalidate(tool_name, result):
            logger.debug(f"write succeeded, invalidating cache: {tool_name}")
            self.invalidate()

        return result

    def invalidate(self, prefix: str = "") -> None:
        """Thread-safe full cache invalidation (called by write tools)."""
        with self._sync_lock:
            if prefix:
                for k in list(self._cache.keys()):
                    if k.startswith(prefix):
                        del self._cache[k]
            else:
                self._cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# SESSION TRACKING
# ─────────────────────────────────────────────────────────────────────────────

class SessionTrackingMiddleware(Middleware):
    """
    Loguje session_id pro každý request — užitečné pro debugging 6 paralelních sessions.

    FastMCP každý stdio process má unikátní session_id — přirozená izolace.
    Tento middleware přidává structured logging pro korelaci logů při debugování.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        session_label = "unknown"
        try:
            if hasattr(context, "fastmcp_context") and context.fastmcp_context:
                session_id = getattr(context.fastmcp_context, "session_id", None)
                if session_id:
                    session_label = session_id[:8]
        except AttributeError:
            pass

        tool_name = "unknown"
        try:
            if isinstance(context.message, dict):
                tool_name = context.message.get("name", "unknown")
        except AttributeError:
            pass

        if settings.log_sessions:
            logger.info(f"[session:{session_label}] {tool_name}")

        return await call_next(context)


# ─────────────────────────────────────────────────────────────────────────────
# MIDDLEWARE STACK BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_middleware_stack(settings) -> list:
    """
    Build the MemPalace middleware stack.

    Order (first in list = innermost on way in, outermost on way back):
    1. ErrorHandlingMiddleware    — catches all uncaught exceptions, logs with traceback
    2. MemPalaceCachingMiddleware — TTL cache for reads + write-aware invalidation
    3. SessionTrackingMiddleware  — session_id logging (runs outermost)
    """
    return [
        ErrorHandlingMiddleware(include_traceback=True),
        MemPalaceCachingMiddleware(),
        SessionTrackingMiddleware(),
    ]
