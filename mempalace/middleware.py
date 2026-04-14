"""
MemPalace FastMCP Middleware Stack
==================================
Phase 2: ResponseCachingMiddleware + EmbedCircuitBreakerMiddleware
Phase 3: SessionTrackingMiddleware + Settings-based TTL configuration
"""

import asyncio
import threading
import time
import logging
from enum import Enum
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext

from .settings import settings

logger = logging.getLogger("mempalace_mcp")


# ═══════════════════════════════════════════════════════════════════
# RESPONSE CACHING MIDDLEWARE
# ═══════════════════════════════════════════════════════════════════

class ResponseCachingMiddleware(Middleware):
    """
    TTL-based response cache for metadata read operations.

    Caches responses for:
    - tools/list (TTL 30s)
    - mempalace_status (TTL 5s)
    - mempalace_list_wings (TTL 30s)
    - mempalace_list_rooms (TTL 30s)
    - mempalace_get_taxonomy (TTL 30s)

    Cache is keyed by tool name + request params.
    """

    TOOL_TTL: dict[str, float] = {
        "mempalace_status": float(settings.cache_ttl_status),
        "mempalace_list_wings": float(settings.cache_ttl_metadata),
        "mempalace_list_rooms": float(settings.cache_ttl_metadata),
        "mempalace_get_taxonomy": float(settings.cache_ttl_metadata),
    }

    def __init__(self):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()  # For sync invalidate()

    def _make_key(self, method: str, params: dict) -> str:
        """Build cache key from tool name and params."""
        if params:
            # Sort for deterministic key
            sorted_params = sorted(params.items())
            param_str = "&".join(f"{k}={v}" for k, v in sorted_params if k != "ctx")
            return f"{method}:{param_str}"
        return method

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        # Only cache specific read tools
        tool_name = context.message.get("name") if isinstance(context.message, dict) else None
        if tool_name not in self.TOOL_TTL:
            return await call_next(context)

        cache_key = self._make_key(
            tool_name,
            context.message if isinstance(context.message, dict) else {},
        )

        # Fast path — check cache WITHOUT lock (Python dict reads are GIL-safe)
        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if time.monotonic() - ts < self.TOOL_TTL.get(tool_name, 30.0):
                logger.debug(f"cache hit: {tool_name}")
                return data
            # expired — del without lock is safe for Python dict
            try:
                del self._cache[cache_key]
            except KeyError:
                pass

        # Slow path — call backend (NO lock held during I/O)
        result = await call_next(context)

        # Store result — short critical section only for write
        if isinstance(result, dict) and "error" not in result:
            async with self._async_lock:
                # Double-check: another request may have stored meanwhile
                if cache_key not in self._cache:
                    self._cache[cache_key] = (result, time.monotonic())

        return result

    def invalidate(self, prefix: str = "") -> None:
        """Thread-safe invalidation for write tools."""
        with self._sync_lock:
            if prefix:
                keys = [k for k in self._cache if k.startswith(prefix)]
                for k in keys:
                    del self._cache[k]
            else:
                self._cache.clear()


# ═══════════════════════════════════════════════════════════════════
# CACHE INVALIDATION MIDDLEWARE
# ═══════════════════════════════════════════════════════════════════

class CacheInvalidationMiddleware(Middleware):
    """
    Listens for write operations and invalidates the response cache.

    Watches for: mempalace_add_drawer, mempalace_remember_code,
    mempalace_consolidate (merge=True), mempalace_diary_write,
    mempalace_delete_drawer, mempalace_kg_add, mempalace_kg_invalidate
    """

    WRITE_TOOLS = {
        "mempalace_add_drawer",
        "mempalace_remember_code",
        "mempalace_consolidate",
        "mempalace_diary_write",
        "mempalace_delete_drawer",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
    }

    def __init__(self, cache: ResponseCachingMiddleware):
        self._cache = cache

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        tool_name = context.message.get("name") if isinstance(context.message, dict) else None

        result = await call_next(context)

        if tool_name in self.WRITE_TOOLS:
            # Invalidate on success for direct-write tools
            should_invalidate = False
            if tool_name in ("mempalace_add_drawer", "mempalace_remember_code",
                             "mempalace_diary_write", "mempalace_delete_drawer",
                             "mempalace_kg_add", "mempalace_kg_invalidate"):
                should_invalidate = isinstance(result, dict) and result.get("success", False)
            elif tool_name == "mempalace_consolidate":
                # consolidate invalidates on success OR when merges happened
                if isinstance(result, dict):
                    should_invalidate = result.get("success", False) or result.get("merged", 0) > 0

            if should_invalidate:
                logger.debug(f"write succeeded, invalidating cache: {tool_name}")
                self._cache.invalidate()

        return result


# ═══════════════════════════════════════════════════════════════════
# EMBED CIRCUIT BREAKER MIDDLEWARE (async conversion of circuit_breaker.py)
# ═══════════════════════════════════════════════════════════════════

class _CBState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class EmbedCircuitBreakerMiddleware(Middleware):
    """
    Async circuit breaker for embed daemon socket operations.

    States:
    - CLOSED: normal operation, failures are counted
    - OPEN: after 5 consecutive failures, rejects requests for 30s
    - HALF_OPEN: after timeout, allows 1 test request
    """

    def __init__(self, failure_threshold=None, recovery_timeout=None):
        self._threshold = failure_threshold if failure_threshold is not None else settings.cb_failure_threshold
        self._recovery_timeout = recovery_timeout if recovery_timeout is not None else settings.cb_recovery_timeout
        self._state = _CBState.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()
        self._half_open_success = False

    @property
    def _current_state(self) -> _CBState:
        if self._state == _CBState.OPEN:
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                return _CBState.HALF_OPEN
        return self._state

    async def record_success(self) -> None:
        async with self._lock:
            if self._state in (_CBState.HALF_OPEN, _CBState.CLOSED):
                self._failures = 0
                if self._state == _CBState.HALF_OPEN:
                    self._state = _CBState.CLOSED
                    logger.info("EmbedCB: HALF_OPEN → CLOSED (daemon healthy)")

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == _CBState.HALF_OPEN:
                self._state = _CBState.OPEN
                self._opened_at = time.monotonic()
                logger.warning("EmbedCB: HALF_OPEN → OPEN (daemon still down)")
            elif self._failures >= self._threshold:
                self._state = _CBState.OPEN
                self._opened_at = time.monotonic()
                logger.warning("EmbedCB: CLOSED → OPEN (%d failures)", self._failures)

    def should_try_socket(self) -> bool:
        """Returns False when circuit is OPEN (embed daemon should be bypassed)."""
        return self._current_state != _CBState.OPEN

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        # The actual circuit state check happens inside backends/lance.py
        # This middleware just provides the async wrapper and logging
        # The circuit state is managed via backends.lance._embed_circuit (sync)
        return await call_next(context)

    def status(self) -> dict:
        s = self._current_state
        return {
            "state": s.value,
            "failures": self._failures,
            "recovery_in": (
                max(0, self._recovery_timeout - (time.monotonic() - self._opened_at))
                if s == _CBState.OPEN else 0
            ),
        }


# ═══════════════════════════════════════════════════════════════════
# SESSION TRACKING MIDDLEWARE
# ═══════════════════════════════════════════════════════════════════


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
        # Získej session_id z FastMCP context pokud je dostupný
        session_label = "unknown"
        try:
            if hasattr(context, "fastmcp_context") and context.fastmcp_context:
                session_id = getattr(context.fastmcp_context, "session_id", None)
                if session_id:
                    session_label = session_id[:8]
        except Exception:
            pass

        tool_name = "unknown"
        try:
            if isinstance(context.message, dict):
                tool_name = context.message.get("name", "unknown")
        except Exception:
            pass

        if settings.log_sessions:
            logger.info(f"[session:{session_label}] {tool_name}")

        return await call_next(context)


# ═══════════════════════════════════════════════════════════════════
# SINGLETON INSTANCES
# ═══════════════════════════════════════════════════════════════════

# NOTE: These singletons are legacy — create_server() creates fresh instances
# via build_middleware_stack(). Kept for backward compatibility (get_*_middleware() getters).
# TODO: deprecate in a future sprint
_caching_middleware = ResponseCachingMiddleware()
_cache_invalidation_middleware = CacheInvalidationMiddleware(_caching_middleware)
_embed_circuit_middleware = EmbedCircuitBreakerMiddleware(
    failure_threshold=settings.cb_failure_threshold,
    recovery_timeout=settings.cb_recovery_timeout,
)
_session_tracking_middleware = SessionTrackingMiddleware()


def build_middleware_stack(settings) -> list:
    """Vytvoří čerstvý middleware stack — volej z create_server()."""
    from fastmcp.server.middleware.caching import ResponseCachingMiddleware, CallToolSettings, ListToolsSettings
    from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
    from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
    from fastmcp.server.middleware.timing import TimingMiddleware

    caching = ResponseCachingMiddleware(
        list_tools_settings=ListToolsSettings(ttl=float(settings.cache_ttl_metadata)),
        call_tool_settings=CallToolSettings(
            ttl=float(settings.cache_ttl_status),
            included_tools=["mempalace_status", "mempalace_list_wings", "mempalace_get_taxonomy", "mempalace_list_rooms"],
        ),
    )
    invalidation = CacheInvalidationMiddleware(caching)
    # NOTE: EmbedCircuitBreakerMiddleware is ready but deactivated.
    # Activation requires should_try_socket() to be wired into backends/lance.py F184.
    embed_cb = EmbedCircuitBreakerMiddleware(
        failure_threshold=settings.cb_failure_threshold,
        recovery_timeout=settings.cb_recovery_timeout,
    )

    return [
        SessionTrackingMiddleware(),
        caching,
        invalidation,
        # embed_cb,  # TODO F184: EmbedCB napojení do backends/lance.py
    ]


def get_caching_middleware() -> ResponseCachingMiddleware:
    return _caching_middleware


def get_cache_invalidation_middleware() -> CacheInvalidationMiddleware:
    return _cache_invalidation_middleware


def get_embed_circuit_middleware() -> EmbedCircuitBreakerMiddleware:
    return _embed_circuit_middleware


def get_session_tracking_middleware() -> SessionTrackingMiddleware:
    return _session_tracking_middleware
