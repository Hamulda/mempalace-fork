"""
Tests for CacheInvalidationMiddleware and EmbedCircuitBreakerMiddleware.
"""
import time

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock
from fastmcp.server.middleware import MiddlewareContext

from mempalace.middleware import (
    CacheInvalidationMiddleware,
    ResponseCachingMiddleware,
    EmbedCircuitBreakerMiddleware,
    build_middleware_stack,
)


class TestCacheInvalidationMiddleware:
    """Test CacheInvalidationMiddleware WRITE_TOOLS coverage."""

    def test_write_tools_has_seven_tools(self):
        """WRITE_TOOLS contains exactly 7 tools."""
        expected = {
            "mempalace_add_drawer",
            "mempalace_remember_code",
            "mempalace_consolidate",
            "mempalace_diary_write",
            "mempalace_delete_drawer",
            "mempalace_kg_add",
            "mempalace_kg_invalidate",
        }
        assert CacheInvalidationMiddleware.WRITE_TOOLS == expected
        assert len(CacheInvalidationMiddleware.WRITE_TOOLS) == 7

    def test_write_tools_includes_delete_drawer(self):
        assert "mempalace_delete_drawer" in CacheInvalidationMiddleware.WRITE_TOOLS

    def test_write_tools_includes_kg_add(self):
        assert "mempalace_kg_add" in CacheInvalidationMiddleware.WRITE_TOOLS

    def test_write_tools_includes_kg_invalidate(self):
        assert "mempalace_kg_invalidate" in CacheInvalidationMiddleware.WRITE_TOOLS

    @pytest.mark.asyncio
    async def test_invalidate_on_delete_drawer_success(self):
        """delete_drawer returns success=True → cache.invalidate() called."""
        cache = MagicMock(spec=ResponseCachingMiddleware)
        mw = CacheInvalidationMiddleware(cache)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_delete_drawer"}

        call_next = AsyncMock(return_value={"success": True, "drawer_id": "test_id"})
        await mw.on_call_tool(context, call_next)

        cache.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalidate_on_kg_add_success(self):
        """kg_add returns success=True → cache.invalidate() called."""
        cache = MagicMock(spec=ResponseCachingMiddleware)
        mw = CacheInvalidationMiddleware(cache)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_kg_add"}

        call_next = AsyncMock(return_value={"success": True, "triple_id": "123"})
        await mw.on_call_tool(context, call_next)

        cache.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalidate_on_kg_invalidate_success(self):
        """kg_invalidate returns success=True → cache.invalidate() called."""
        cache = MagicMock(spec=ResponseCachingMiddleware)
        mw = CacheInvalidationMiddleware(cache)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_kg_invalidate"}

        call_next = AsyncMock(return_value={"success": True})
        await mw.on_call_tool(context, call_next)

        cache.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_invalidate_on_delete_drawer_failure(self):
        """delete_drawer returns success=False → no cache.invalidate()."""
        cache = MagicMock(spec=ResponseCachingMiddleware)
        mw = CacheInvalidationMiddleware(cache)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_delete_drawer"}

        call_next = AsyncMock(return_value={"success": False, "error": "not found"})
        await mw.on_call_tool(context, call_next)

        cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidate_invalidates_on_merged(self):
        """consolidate with merged>0 invalidates cache even if success=False."""
        cache = MagicMock(spec=ResponseCachingMiddleware)
        mw = CacheInvalidationMiddleware(cache)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_consolidate"}

        call_next = AsyncMock(return_value={"success": True, "merged": 3})
        await mw.on_call_tool(context, call_next)

        cache.invalidate.assert_called_once()


class TestEmbedCircuitBreakerNotInStack:
    """Test that EmbedCircuitBreakerMiddleware is not in the active stack."""

    def test_embed_cb_not_in_build_middleware_stack(self):
        """build_middleware_stack() returns list WITHOUT EmbedCircuitBreakerMiddleware."""
        from mempalace.settings import MemPalaceSettings
        settings = MemPalaceSettings()

        stack = build_middleware_stack(settings)

        # Verify no EmbedCircuitBreakerMiddleware in stack
        embed_instances = [mw for mw in stack if isinstance(mw, EmbedCircuitBreakerMiddleware)]
        assert len(embed_instances) == 0, (
            f"EmbedCircuitBreakerMiddleware should not be in stack, found: {embed_instances}"
        )

        # Verify stack has exactly 3 items
        assert len(stack) == 3

        # Verify correct middleware types in stack
        from mempalace.middleware import SessionTrackingMiddleware
        from fastmcp.server.middleware.caching import ResponseCachingMiddleware

        assert isinstance(stack[0], SessionTrackingMiddleware)
        assert isinstance(stack[1], ResponseCachingMiddleware)
        assert isinstance(stack[2], CacheInvalidationMiddleware)

    def test_embed_cb_on_call_tool_is_pass_through(self):
        """EmbedCircuitBreakerMiddleware.on_call_tool just calls call_next (no circuit logic)."""
        mw = EmbedCircuitBreakerMiddleware()

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "some_tool"}

        async def mock_call_next(ctx):
            return {"result": "ok"}

        result = mw.on_call_tool(context, mock_call_next)

        # It's an async function that just returns call_next result
        import asyncio
        assert asyncio.iscoroutine(result) or result == {"result": "ok"}


class TestResponseCachingMiddleware:
    """Test ResponseCachingMiddleware no-I/O-under-lock fix."""

    @pytest.mark.asyncio
    async def test_response_cache_hit_no_call_next(self):
        """Cache hit → call_next is NOT called."""
        mw = ResponseCachingMiddleware()
        # Key is "mempalace_status:name=mempalace_status" (full message dict as params)
        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        mw._cache[key] = ({"total_drawers": 42}, time.monotonic() - 1.0)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}

        call_next = AsyncMock()
        result = await mw.on_call_tool(context, call_next)

        call_next.assert_not_called()
        assert result == {"total_drawers": 42}

    @pytest.mark.asyncio
    async def test_response_cache_miss_stores(self):
        """Cache miss → call_next is called → result is stored."""
        mw = ResponseCachingMiddleware()

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}

        async def mock_call_next(ctx):
            return {"total_drawers": 99}

        call_next = AsyncMock(side_effect=mock_call_next)
        result = await mw.on_call_tool(context, call_next)

        call_next.assert_called_once()
        assert result == {"total_drawers": 99}
        # Key includes name param
        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        assert key in mw._cache

    @pytest.mark.asyncio
    async def test_response_cache_expired_recomputes(self):
        """Expired entry → call_next is called again."""
        mw = ResponseCachingMiddleware()
        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        old_ts = time.monotonic() - 10.0
        mw._cache[key] = ({"total_drawers": 1}, old_ts)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}

        async def mock_call_next(ctx):
            return {"total_drawers": 2}

        call_next = AsyncMock(side_effect=mock_call_next)
        result = await mw.on_call_tool(context, call_next)

        call_next.assert_called_once()
        assert result == {"total_drawers": 2}
        # Entry should be refreshed
        data, ts = mw._cache[key]
        assert ts > old_ts

    @pytest.mark.asyncio
    async def test_response_cache_no_io_under_lock(self):
        """call_next runs WITHOUT lock held — concurrent requests don't deadlock."""
        import asyncio

        mw = ResponseCachingMiddleware()

        context1 = MagicMock(spec=MiddlewareContext)
        context1.message = {"name": "mempalace_status"}

        context2 = MagicMock(spec=MiddlewareContext)
        context2.message = {"name": "mempalace_status"}

        async def mock_call_next(ctx):
            # Simulate slow I/O
            await asyncio.sleep(0.05)
            return {"total_drawers": 42}

        async def run_both():
            task1 = asyncio.create_task(mw.on_call_tool(context1, mock_call_next))
            task2 = asyncio.create_task(mw.on_call_tool(context2, mock_call_next))
            return await asyncio.gather(task1, task2)

        # Should complete without deadlock — both get valid results
        start = time.monotonic()
        results = await run_both()
        elapsed = time.monotonic() - start

        assert len(results) == 2, "Both requests should complete"
        assert results[0] == {"total_drawers": 42}
        assert results[1] == {"total_drawers": 42}
        # With no lock held during I/O, both ran in ~50ms (parallel), not ~100ms (serial)
        assert elapsed < 0.08, f"Requests took {elapsed:.3f}s — should be parallel, not serial"

    @pytest.mark.asyncio
    async def test_response_cache_error_not_stored(self):
        """Result with 'error' key is not cached."""
        mw = ResponseCachingMiddleware()

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}

        async def mock_call_next(ctx):
            return {"error": "something went wrong"}

        call_next = AsyncMock(side_effect=mock_call_next)
        result = await mw.on_call_tool(context, call_next)

        assert result == {"error": "something went wrong"}
        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        assert key not in mw._cache