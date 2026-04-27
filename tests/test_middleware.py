"""
Tests for MemPalace Middleware Stack (FastMCP v2.9 ErrorHandling + custom caching).
"""
import time

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware

from mempalace.middleware import (
    MemPalaceCachingMiddleware,
    SessionTrackingMiddleware,
    ErrorHandlingMiddleware,
    _should_invalidate,
    _WRITE_TOOLS,
    build_middleware_stack,
)


# ─────────────────────────────────────────────────────────────────────────────
# _should_invalidate + _WRITE_TOOLS
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldInvalidate:
    """Test _should_invalidate logic (replaces CacheInvalidationMiddleware tests)."""

    def test_write_tools_has_seven_tools(self):
        """_WRITE_TOOLS set contains exactly 7 tools."""
        expected = {
            "mempalace_add_drawer",
            "mempalace_remember_code",
            "mempalace_consolidate",
            "mempalace_diary_write",
            "mempalace_delete_drawer",
            "mempalace_kg_add",
            "mempalace_kg_invalidate",
        }
        assert _WRITE_TOOLS == expected
        assert len(_WRITE_TOOLS) == 7

    def test_delete_drawer_success_invalidates(self):
        assert _should_invalidate("mempalace_delete_drawer", {"success": True, "drawer_id": "x"}) is True

    def test_delete_drawer_failure_no_invalidate(self):
        assert _should_invalidate("mempalace_delete_drawer", {"success": False}) is False

    def test_kg_add_success_invalidates(self):
        assert _should_invalidate("mempalace_kg_add", {"success": True, "triple_id": "123"}) is True

    def test_kg_add_failure_no_invalidate(self):
        assert _should_invalidate("mempalace_kg_add", {"success": False}) is False

    def test_kg_invalidate_success_invalidates(self):
        assert _should_invalidate("mempalace_kg_invalidate", {"success": True}) is True

    def test_consolidate_success_invalidates(self):
        assert _should_invalidate("mempalace_consolidate", {"success": True, "merged": 0}) is True

    def test_consolidate_merged_invalidates(self):
        assert _should_invalidate("mempalace_consolidate", {"success": False, "merged": 3}) is True

    def test_consolidate_no_merged_no_invalidate(self):
        assert _should_invalidate("mempalace_consolidate", {"success": False, "merged": 0}) is False

    def test_read_tool_no_invalidate(self):
        assert _should_invalidate("mempalace_status", {"success": True}) is False
        assert _should_invalidate("mempalace_list_wings", {"total_wings": 3}) is False

    def test_unknown_tool_no_invalidate(self):
        assert _should_invalidate("some_unknown_tool", {"success": True}) is False


# ─────────────────────────────────────────────────────────────────────────────
# MemPalaceCachingMiddleware
# ─────────────────────────────────────────────────────────────────────────────

class TestMemPalaceCachingMiddleware:
    """Test MemPalaceCachingMiddleware public contract."""

    @pytest.mark.asyncio
    async def test_cache_hit_skips_call_next(self):
        """Cache hit → call_next is NOT called."""
        mw = MemPalaceCachingMiddleware()
        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        mw._cache[key] = ({"total_drawers": 42}, time.monotonic() - 1.0)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}

        call_next = AsyncMock()
        result = await mw.on_call_tool(context, call_next)

        call_next.assert_not_called()
        assert result == {"total_drawers": 42}

    @pytest.mark.asyncio
    async def test_cache_miss_calls_call_next(self):
        """Cache miss → call_next is called and result is stored."""
        mw = MemPalaceCachingMiddleware()

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}

        async def mock_call_next(ctx):
            return {"total_drawers": 99}

        call_next = AsyncMock(side_effect=mock_call_next)
        result = await mw.on_call_tool(context, call_next)

        call_next.assert_called_once()
        assert result == {"total_drawers": 99}
        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        assert key in mw._cache

    @pytest.mark.asyncio
    async def test_cache_expired_recomputes(self):
        """Expired entry → call_next is called again."""
        mw = MemPalaceCachingMiddleware()
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
        data, ts = mw._cache[key]
        assert ts > old_ts

    @pytest.mark.asyncio
    async def test_non_cached_tool_bypasses(self):
        """Tool not in TOOL_TTL → call_next called, no caching."""
        mw = MemPalaceCachingMiddleware()

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_add_drawer"}

        async def mock_call_next(ctx):
            return {"success": True}

        call_next = AsyncMock(side_effect=mock_call_next)
        result = await mw.on_call_tool(context, call_next)

        call_next.assert_called_once()
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_write_success_triggers_invalidate(self):
        """Write tool success → cache invalidated."""
        mw = MemPalaceCachingMiddleware()

        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        mw._cache[key] = ({"total_drawers": 42}, time.monotonic() - 1.0)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_add_drawer"}

        async def mock_call_next(ctx):
            return {"success": True}

        call_next = AsyncMock(side_effect=mock_call_next)
        await mw.on_call_tool(context, call_next)

        assert len(mw._cache) == 0

    @pytest.mark.asyncio
    async def test_write_failure_no_invalidate(self):
        """Write tool failure → cache NOT invalidated."""
        mw = MemPalaceCachingMiddleware()

        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        mw._cache[key] = ({"total_drawers": 42}, time.monotonic() - 1.0)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_add_drawer"}

        async def mock_call_next(ctx):
            return {"success": False, "error": "perm denied"}

        call_next = AsyncMock(side_effect=mock_call_next)
        await mw.on_call_tool(context, call_next)

        assert key in mw._cache

    @pytest.mark.asyncio
    async def test_consolidate_merged_triggers_invalidate(self):
        """consolidate with merged>0 → cache invalidated even if success=False."""
        mw = MemPalaceCachingMiddleware()

        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        mw._cache[key] = ({"total_drawers": 42}, time.monotonic() - 1.0)

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_consolidate"}

        async def mock_call_next(ctx):
            return {"success": False, "merged": 3}

        call_next = AsyncMock(side_effect=mock_call_next)
        await mw.on_call_tool(context, call_next)

        assert len(mw._cache) == 0

    @pytest.mark.asyncio
    async def test_error_result_not_stored(self):
        """Result with 'error' key is not cached."""
        mw = MemPalaceCachingMiddleware()

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}

        async def mock_call_next(ctx):
            return {"error": "something went wrong"}

        call_next = AsyncMock(side_effect=mock_call_next)
        result = await mw.on_call_tool(context, call_next)

        assert result == {"error": "something went wrong"}
        key = mw._make_key("mempalace_status", {"name": "mempalace_status"})
        assert key not in mw._cache

    @pytest.mark.asyncio
    async def test_no_io_under_lock(self):
        """call_next runs WITHOUT lock held — concurrent requests don't deadlock."""
        import asyncio

        mw = MemPalaceCachingMiddleware()

        context1 = MagicMock(spec=MiddlewareContext)
        context1.message = {"name": "mempalace_status"}

        context2 = MagicMock(spec=MiddlewareContext)
        context2.message = {"name": "mempalace_status"}

        async def mock_call_next(ctx):
            await asyncio.sleep(0.05)
            return {"total_drawers": 42}

        async def run_both():
            task1 = asyncio.create_task(mw.on_call_tool(context1, mock_call_next))
            task2 = asyncio.create_task(mw.on_call_tool(context2, mock_call_next))
            return await asyncio.gather(task1, task2)

        start = time.monotonic()
        results = await run_both()
        elapsed = time.monotonic() - start

        assert len(results) == 2
        assert results[0] == {"total_drawers": 42}
        assert results[1] == {"total_drawers": 42}
        assert elapsed < 0.08, f"Requests took {elapsed:.3f}s — should be parallel"


# ─────────────────────────────────────────────────────────────────────────────
# Middleware Stack
# ─────────────────────────────────────────────────────────────────────────────

class TestMiddlewareStack:
    """Test build_middleware_stack structure."""

    def test_stack_has_three_middleware(self):
        from mempalace.settings import MemPalaceSettings
        settings = MemPalaceSettings()
        stack = build_middleware_stack(settings)
        assert len(stack) == 3

    def test_stack_order(self):
        from mempalace.settings import MemPalaceSettings
        settings = MemPalaceSettings()
        stack = build_middleware_stack(settings)
        assert isinstance(stack[0], ErrorHandlingMiddleware)
        assert isinstance(stack[1], MemPalaceCachingMiddleware)
        assert isinstance(stack[2], SessionTrackingMiddleware)


# ─────────────────────────────────────────────────────────────────────────────
# SessionTrackingMiddleware
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionTrackingMiddleware:
    """Test SessionTrackingMiddleware."""

    @pytest.mark.asyncio
    async def test_passes_through_call_next(self):
        mw = SessionTrackingMiddleware()

        context = MagicMock(spec=MiddlewareContext)
        context.message = {"name": "mempalace_status"}
        context.fastmcp_context = None

        async def mock_call_next(ctx):
            return {"total_drawers": 42}

        call_next = AsyncMock(side_effect=mock_call_next)
        result = await mw.on_call_tool(context, call_next)

        call_next.assert_called_once()
        assert result == {"total_drawers": 42}
