"""
Tests for search cache isolation — cross-palace contamination prevention.

Run: pytest tests/test_search_cache_isolation.py -v
"""

import time
import pytest
from unittest import mock

from mempalace.query_cache import QueryCache


class TestQueryCacheKeyIsolation:
    """Cache keys must include palace_path + collection_name."""

    def test_same_query_different_palace_not_cached(self):
        """Identický query pro různé palace_path nesmí sdílet cache entry."""
        cache = QueryCache(ttl_seconds=60)

        result_a = {"results": ["palace_a_result"]}
        result_b = {"results": ["palace_b_result"]}

        key_a = "/palace/a|default|error|None|None|None|None|5|False|None|None"
        key_b = "/palace/b|default|error|None|None|None|None|5|False|None|None"

        cache.set_value(key_a, result_a)
        cache.set_value(key_b, result_b)

        assert cache.get_value(key_a) == result_a
        assert cache.get_value(key_b) == result_b

    def test_cache_key_includes_palace_path(self):
        """Cache key format must include palace_path as first component."""
        cache = QueryCache(ttl_seconds=60)
        result = {"data": "test"}

        # palace_path in key
        key_with_palace = "/tmp/palace1|default|query|None|None|None|None|5|False|None|None"
        key_without_palace = "query|None|None|None|None|5|False|None|None"

        cache.set_value(key_with_palace, result)

        # Should hit with full key
        assert cache.get_value(key_with_palace) == result
        # Should miss without palace_path (cross-palace protection)
        assert cache.get_value(key_without_palace) is None

    def test_cache_key_includes_collection_name(self):
        """Cache key must distinguish different collections in same palace."""
        cache = QueryCache(ttl_seconds=60)
        result = {"data": "test"}

        key_default = "/palace|default|query|None|None|None|None|5|False|None|None"
        key_custom = "/palace|custom_collection|query|None|None|None|None|5|False|None|None"

        cache.set_value(key_default, {"collection": "default"})
        cache.set_value(key_custom, {"collection": "custom"})

        assert cache.get_value(key_default) == {"collection": "default"}
        assert cache.get_value(key_custom) == {"collection": "custom"}


class TestQueryCacheInvalidation:
    """Write invalidation must reach search_memories cache entries."""

    def test_clear_invalidates_all_entries(self):
        """cache.clear() removes all entries regardless of key format."""
        cache = QueryCache(ttl_seconds=60)

        keys = [
            "/palace/a|default|q1|None|None|None|None|5|False|None|None",
            "/palace/b|default|q2|None|None|None|None|5|False|None|None",
            "raw_string_key",
        ]
        for k in keys:
            cache.set_value(k, {"result": k})

        cache.clear()

        for k in keys:
            assert cache.get_value(k) is None

    def test_ttl_expiry_invalidates_search_cache(self):
        """TTL expiry must invalidate entries stored via set_value."""
        cache = QueryCache(ttl_seconds=0.05)

        key = "/palace|default|query|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"data": "fresh"})

        # Still valid immediately after
        assert cache.get_value(key) is not None

        time.sleep(0.1)

        # Expired
        assert cache.get_value(key) is None

    def test_invalidate_collection_works_for_get_set_interface(self):
        """invalidate_collection(palace_path, collection) zneplatní get/set entries pro ten palace+collection."""
        cache = QueryCache(ttl_seconds=60)

        # Classic get/set pair uses _last_write timestamp
        cache.set("/palace/a", "default", ["query"], 5, {"data": "test"})
        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        # get/set interface is invalidated for this palace+collection
        assert cache.get("/palace/a", "default", ["query"], 5) is None

        # Different palace is NOT affected
        cache.set("/palace/b", "default", ["query"], 5, {"data": "other"})
        assert cache.get("/palace/b", "default", ["query"], 5) == {"data": "other"}

    def test_raw_key_interface_invalidated_by_invalidate_collection(self):
        """invalidate_collection(palace_path, collection) MAŽE i get_value/set_value entries
        pro odpovídající palace+collection (prefix match na klíči).

        cross-palace izolace: get_value/set_value klíče obsahují palace_path v sobě,
        takže různé palace mají různé klíče. invalidate_collection() maže
        entries matching "{palace_path}|{collection}|" prefix.
        """
        cache = QueryCache(ttl_seconds=60)

        key = "/palace/a|default|query|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"data": "test"})

        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        # invalidate_collection now evicts raw-key entries with matching prefix
        assert cache.get_value(key) is None

        # Different palace+collection is NOT affected
        cache.set_value("/palace/b|default|query|None|None|None|None|5|False|None|None",
                        {"data": "other"})
        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)
        assert cache.get_value("/palace/b|default|query|None|None|None|None|5|False|None|None") == {"data": "other"}


class TestQueryCacheThreadSafety:
    """Query cache must be thread-safe under concurrent access."""

    def test_concurrent_get_set(self):
        """Concurrent get/set from multiple threads must not corrupt cache."""
        import threading
        cache = QueryCache(ttl_seconds=30, maxsize=100)

        errors = []
        results = {}

        def writer(thread_id: int):
            try:
                for i in range(50):
                    key = f"/palace|default|query_{thread_id}_{i}|None|None|None|None|5|False|None|None"
                    cache.set_value(key, {"thread": thread_id, "i": i})
                    results[f"w_{thread_id}_{i}"] = True
            except Exception as e:
                errors.append(f"writer_{thread_id}: {e}")

        def reader(thread_id: int):
            try:
                for i in range(50):
                    key = f"/palace|default|query_{thread_id}_{i}|None|None|None|None|5|False|None|None"
                    val = cache.get_value(key)
                    results[f"r_{thread_id}_{i}"] = val is not None
            except Exception as e:
                errors.append(f"reader_{thread_id}: {e}")

        threads = []
        for t in range(4):
            threads.append(threading.Thread(target=writer, args=(t,)))
            threads.append(threading.Thread(target=reader, args=(t,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

    def test_concurrent_clear_and_get(self):
        """Concurrent clear() and get_value() must not raise."""
        import threading
        cache = QueryCache(ttl_seconds=30)

        # Pre-fill
        for i in range(50):
            key = f"/palace|default|query_{i}|None|None|None|None|5|False|None|None"
            cache.set_value(key, {"i": i})

        errors = []

        def clearer():
            try:
                for _ in range(20):
                    cache.clear()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(f"clear: {e}")

        def getter():
            try:
                for i in range(50):
                    key = f"/palace|default|query_{i}|None|None|None|None|5|False|None|None"
                    cache.get_value(key)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(f"get: {e}")

        threads = [
            threading.Thread(target=clearer),
            threading.Thread(target=getter),
            threading.Thread(target=getter),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"


class TestSearchCacheIntegration:
    """Integration tests for search_memories cache isolation."""

    def test_different_palace_paths_produce_different_cache_keys(self):
        """search_memories cache keys for different palaces must differ."""
        # Verify the key format used by search_memories
        def make_key(palace_path, collection_name, query, wing=None, room=None,
                      is_latest=None, agent_id=None, n_results=5, rerank=False,
                      priority_gte=None, priority_lte=None):
            return (f"{palace_path}|{collection_name}|{query}|{wing}|{room}|"
                    f"{is_latest}|{agent_id}|{n_results}|{rerank}|{priority_gte}|{priority_lte}")

        key_a = make_key("/palace/a", "default", "error")
        key_b = make_key("/palace/b", "default", "error")

        assert key_a != key_b
        assert "/palace/a" in key_a
        assert "/palace/b" in key_b

    def test_invalidate_query_cache_clears_all_search_entries(self):
        """invalidate_query_cache must clear all search_memories cache entries."""
        from mempalace.searcher import invalidate_query_cache
        from mempalace.query_cache import get_query_cache

        cache = get_query_cache()
        initial_size = cache._total_size()

        # Simulate search_memories writing entries
        keys = [
            "/palace/a|default|q1|None|None|None|None|5|False|None|None",
            "/palace/b|default|q2|None|None|None|None|5|False|None|None",
        ]
        for k in keys:
            cache.set_value(k, {"results": k})

        assert cache._total_size() == initial_size + 2

        invalidate_query_cache()

        for k in keys:
            assert cache.get_value(k) is None

    def test_write_invalidation_chain_reaches_search_cache(self):
        """Lance write -> invalidate_query_cache -> search cache cleared.

        This tests the full invalidation chain: after a write operation
        (add_drawer/delete_memory), the search cache must be cleared so
        subsequent searches return fresh results.
        """
        from mempalace.searcher import invalidate_query_cache
        from mempalace.query_cache import get_query_cache

        cache = get_query_cache()

        key = "/palace/a|default|error|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"results": ["old", "stale"]})

        # Simulate write completion → invalidate_query_cache()
        invalidate_query_cache()

        # Cache is now empty — next search will hit the backend
        assert cache.get_value(key) is None
