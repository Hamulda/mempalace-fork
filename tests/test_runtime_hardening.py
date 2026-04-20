"""
Tests for runtime lifecycle hardening: MemoryGuard restart semantics,
cache invalidation behavior, singleton cleanup, no stale shared resource behavior.

Run: pytest tests/test_runtime_hardening.py -v
"""

import time
import threading
import pytest
from unittest import mock

from mempalace.memory_guard import MemoryGuard, MemoryPressure


# =============================================================================
# MemoryGuard RESTART SEMANTICS
# =============================================================================

class TestMemoryGuardRestartSemantics:
    """
    MemoryGuard.stop() must reset _started so the next get() properly blocks
    until first measurement — otherwise a restart after stop() returns
    immediately without waiting for first reading.
    """

    def test_stop_resets_started_event(self):
        """stop() must clear _started so next get() blocks on fresh measurement."""
        # Reset singleton
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            return_value=(MemoryPressure.NOMINAL, 0.5),
        ):
            guard = MemoryGuard.get()
            assert MemoryGuard._started.is_set()
            assert guard.pressure == MemoryPressure.NOMINAL

            guard.stop()

            # After stop(), _started must be cleared
            assert not MemoryGuard._started.is_set(), (
                "_started should be cleared by stop() so next get() blocks"
            )

        # Cleanup
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

    def test_get_after_stop_waits_for_first_measurement(self):
        """get() after stop() must block until first measurement completes."""
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        call_count = [0]

        def mock_pressure():
            call_count[0] += 1
            if call_count[0] == 1:
                return (MemoryPressure.WARN, 0.8)
            return (MemoryPressure.NOMINAL, 0.6)

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            side_effect=mock_pressure,
        ):
            # First instance
            guard1 = MemoryGuard.get()
            assert guard1.pressure == MemoryPressure.WARN
            assert guard1.used_ratio == 0.8

            guard1.stop()

            # Second instance after stop — must wait for first measurement
            guard2 = MemoryGuard.get()
            # After stop+get, must be fully initialized (not default NOMINAL)
            assert guard2 is not guard1
            # Must reflect real measurement, not default
            assert guard2.used_ratio > 0  # real measurement
            assert MemoryGuard._started.is_set()

        MemoryGuard._instance = None
        MemoryGuard._started.clear()

    def test_restart_creates_new_instance(self):
        """stop() + get() must create a new instance, not reuse stopped one."""
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            return_value=(MemoryPressure.WARN, 0.75),
        ):
            guard1 = MemoryGuard.get()
            assert guard1.pressure == MemoryPressure.WARN

            guard1.stop()

            with mock.patch(
                "mempalace.memory_guard._get_memory_pressure_macos",
                return_value=(MemoryPressure.CRITICAL, 0.9),
            ):
                guard2 = MemoryGuard.get()
                assert guard2 is not guard1
                assert guard2.pressure == MemoryPressure.CRITICAL

        MemoryGuard._instance = None
        MemoryGuard._started.clear()

    def test_multiple_get_calls_before_startup_returns_same_instance(self):
        """Multiple get() calls before first measurement returns same blocking instance."""
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            return_value=(MemoryPressure.NOMINAL, 0.4),
        ):
            results = []
            barrier = threading.Barrier(3)

            def getter(idx):
                g = MemoryGuard.get()
                results.append((idx, g.pressure, g.used_ratio))
                barrier.wait()

            threads = [
                threading.Thread(target=getter, args=(0,)),
                threading.Thread(target=getter, args=(1,)),
                threading.Thread(target=getter, args=(2,)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All got same instance (singleton)
            assert len({id(r) for _, r, _ in results}) == 1
            # All saw same pressure
            assert all(p == MemoryPressure.NOMINAL for _, p, _ in results)

        MemoryGuard._instance = None
        MemoryGuard._started.clear()


# =============================================================================
# Cache invalidation behavior
# =============================================================================

class TestCacheInvalidationBehavior:
    """Per-collection invalidation vs global clear semantics."""

    def test_search_memories_cache_respects_last_write_on_get_value(self):
        """search_memories cache entries with palace_path+collection self-evict on stale."""
        from mempalace.query_cache import QueryCache

        cache = QueryCache(ttl_seconds=60)
        palace = "/tmp/test_palace"
        collection = "default"

        # Set a cache entry as if from search_memories
        key = f"{palace}|{collection}|error|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"results": "cached"}, palace_path=palace, collection=collection)

        # Verify it's cached
        assert cache.get_value(key, palace_path=palace, collection=collection) == {"results": "cached"}

        # Invalidate collection (simulating a write operation)
        cache.invalidate_collection(palace, collection)
        time.sleep(0.001)

        # Now get_value must return None because _last_write > entry timestamp
        assert cache.get_value(key, palace_path=palace, collection=collection) is None, (
            "get_value with palace_path+collection must check _last_write"
        )

    def test_get_value_without_palace_args_after_raw_key_invalidation(self):
        """
        invalidate_collection removes raw-string-key entries directly from _cache
        (by prefix scan), so get_value returns None after invalidation regardless
        of whether palace args are passed.

        This is the correct behavior: invalidate_collection removes entries for
        that (palace, collection) pair so subsequent searches always see fresh data.
        """
        from mempalace.query_cache import QueryCache

        cache = QueryCache(ttl_seconds=60)
        palace = "/tmp/test_palace"
        collection = "default"

        key = f"{palace}|{collection}|error"

        # Store entry (raw string key with palace|collection embedded in key)
        cache.set_value(key, {"results": "cached"}, palace_path=palace, collection=collection)

        # Confirm it's cached
        assert cache.get_value(key, palace_path=palace, collection=collection) == {"results": "cached"}

        # Invalidate collection — this directly removes the key from _cache
        cache.invalidate_collection(palace, collection)
        time.sleep(0.001)

        # After invalidate_collection, entry is gone for ALL callers
        # (both WITH and WITHOUT palace args) because the key was deleted from _cache
        assert cache.get_value(key, palace_path=palace, collection=collection) is None
        assert cache.get_value(key) is None, (
            "invalidate_collection removes raw-string-key entries from _cache directly; "
            "no bypass exists post-invalidation"
        )

    def test_invalidate_collection_affects_only_that_palace_collection(self):
        """invalidate_collection only marks one (palace, collection) pair, not all."""
        from mempalace.query_cache import QueryCache

        cache = QueryCache(ttl_seconds=60)

        key_a = "/palace/a|col|q"
        key_b = "/palace/b|col|q"

        cache.set_value(key_a, {"data": "A"}, palace_path="/palace/a", collection="col")
        cache.set_value(key_b, {"data": "B"}, palace_path="/palace/b", collection="col")

        # Invalidate only palace A
        cache.invalidate_collection("/palace/a", "col")
        time.sleep(0.001)

        # palace A entry must be evicted
        assert cache.get_value(key_a, palace_path="/palace/a", collection="col") is None
        # palace B entry must remain
        assert cache.get_value(key_b, palace_path="/palace/b", collection="col") == {"data": "B"}

    def test_clear_removes_all_entries_regardless_of_last_write(self):
        """clear() nukes everything regardless of _last_write state."""
        from mempalace.query_cache import QueryCache

        cache = QueryCache(ttl_seconds=60)
        palace = "/tmp/p"

        key = f"{palace}|c|q"
        cache.set_value(key, {"data": "test"}, palace_path=palace, collection="c")
        cache.invalidate_collection(palace, "c")
        time.sleep(0.001)

        # clear() should remove it
        cache.clear()
        assert cache.get_value(key) is None

    def test_search_memories_write_invalidation_flow(self):
        """Simulate: write → invalidate_collection → subsequent search sees fresh data."""
        from mempalace.query_cache import QueryCache

        cache = QueryCache(ttl_seconds=60)
        palace = "/tmp/my_palace"
        collection = "default"
        key = f"{palace}|{collection}|my query|None|None|None|None|5|False|None|None"

        # First search: cache miss
        result1 = cache.get_value(key, palace_path=palace, collection=collection)
        assert result1 is None

        # Cache the result
        cache.set_value(key, {"results": ["old_entry"]}, palace_path=palace, collection=collection)

        # Second search: cache hit (old entry)
        result2 = cache.get_value(key, palace_path=palace, collection=collection)
        assert result2 == {"results": ["old_entry"]}

        # Write happens → invalidate_collection
        cache.invalidate_collection(palace, collection)
        time.sleep(0.001)

        # Third search: cache miss (stale entry evicted)
        result3 = cache.get_value(key, palace_path=palace, collection=collection)
        assert result3 is None, "write should make cached entry stale"

        # New write's results cached
        cache.set_value(key, {"results": ["new_entry"]}, palace_path=palace, collection=collection)

        # Fourth search: cache hit (new entry)
        result4 = cache.get_value(key, palace_path=palace, collection=collection)
        assert result4 == {"results": ["new_entry"]}


# =============================================================================
# Singleton cleanup
# =============================================================================

class TestSingletonCleanup:
    """Verify singleton resources properly clean up state on stop/reset."""

    def test_memory_guard_stop_clears_instance_and_started(self):
        """stop() must null out _instance AND clear _started."""
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            return_value=(MemoryPressure.NOMINAL, 0.5),
        ):
            guard = MemoryGuard.get()
            assert guard is not None

            guard.stop()

            assert MemoryGuard._instance is None
            assert not MemoryGuard._started.is_set()

        MemoryGuard._instance = None
        MemoryGuard._started.clear()

    def test_memory_guard_singleton_returns_same_instance(self):
        """get() always returns same instance when already running."""
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            return_value=(MemoryPressure.NOMINAL, 0.5),
        ):
            g1 = MemoryGuard.get()
            g2 = MemoryGuard.get()
            assert g1 is g2

        MemoryGuard._instance = None
        MemoryGuard._started.clear()


# =============================================================================
# No stale shared resource behavior
# =============================================================================

class TestNoStaleSharedResources:
    """
    Verify no stale shared resources leak across restart cycles.
    Key invariant: after stop(), new get() must not reuse any old state.
    """

    def test_memory_guard_pressure_reset_after_stop(self):
        """After stop(), new instance must have fresh pressure from measurement."""
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            return_value=(MemoryPressure.CRITICAL, 0.95),
        ):
            guard1 = MemoryGuard.get()
            assert guard1.pressure == MemoryPressure.CRITICAL

            guard1.stop()

            with mock.patch(
                "mempalace.memory_guard._get_memory_pressure_macos",
                return_value=(MemoryPressure.NOMINAL, 0.4),
            ):
                guard2 = MemoryGuard.get()
                assert guard2.pressure == MemoryPressure.NOMINAL
                assert guard2.used_ratio == 0.4

        MemoryGuard._instance = None
        MemoryGuard._started.clear()

    def test_query_cache_clear_removes_stale_entries_across_palaces(self):
        """clear() removes entries from all palaces."""
        from mempalace.query_cache import QueryCache

        cache = QueryCache(ttl_seconds=60)

        keys = [
            ("/palace/a", "col", "q1"),
            ("/palace/b", "col", "q2"),
            ("/palace/c", "other", "q3"),
        ]
        for palace, collection, q in keys:
            key = f"{palace}|{collection}|{q}"
            cache.set_value(key, {"data": q}, palace_path=palace, collection=collection)

        cache.clear()

        for palace, collection, q in keys:
            key = f"{palace}|{collection}|{q}"
            assert cache.get_value(key) is None

    def test_invalidate_collection_isolation_between_different_collections(self):
        """Different collections in same palace are independently invalidated."""
        from mempalace.query_cache import QueryCache

        cache = QueryCache(ttl_seconds=60)
        palace = "/tmp/p"

        cache.set_value("key1", {"d": "1"}, palace_path=palace, collection="col1")
        cache.set_value("key2", {"d": "2"}, palace_path=palace, collection="col2")

        cache.invalidate_collection(palace, "col1")
        time.sleep(0.001)

        # col1 is invalidated
        assert cache.get_value("key1", palace_path=palace, collection="col1") is None
        # col2 is not affected
        assert cache.get_value("key2", palace_path=palace, collection="col2") == {"d": "2"}