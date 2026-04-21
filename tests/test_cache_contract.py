"""
Tests for the canonical cache contract: invalidation story, stale prevention,
and cross-palace isolation.

Run: pytest tests/test_cache_contract.py -v
"""

import time
import pytest
import threading

from mempalace.query_cache import QueryCache, get_query_cache
from mempalace.searcher import invalidate_query_cache, invalidate_all_caches


class TestInvalidateCollectionBehavior:
    """Canonical behavior: invalidate_collection evicts both interfaces."""

    def test_invalidate_collection_evicts_get_set_interface(self):
        """invalidate_collection evicts entries stored via get()/set()."""
        cache = QueryCache(ttl_seconds=60)
        cache.set("/palace/a", "default", ["query"], 5, {"data": "test"})

        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        assert cache.get("/palace/a", "default", ["query"], 5) is None

    def test_invalidate_collection_evicts_get_value_interface(self):
        """invalidate_collection evicts entries stored via get_value()/set_value()
        when palace_path+collection prefix matches the key."""
        cache = QueryCache(ttl_seconds=60)
        key = "/palace/a|default|error|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"data": "test"})

        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        assert cache.get_value(key) is None

    def test_invalidate_collection_prefix_match_raw_keys(self):
        """Raw key prefix match: 'palace|col|' prefix determines eviction."""
        cache = QueryCache(ttl_seconds=60)

        # Two entries in same palace, different collections
        key_col1 = "/palace/a|col1|query|None|None|None|None|5|False|None|None"
        key_col2 = "/palace/a|col2|query|None|None|None|None|5|False|None|None"
        cache.set_value(key_col1, {"col": "col1"})
        cache.set_value(key_col2, {"col": "col2"})

        # Invalidate only col1
        cache.invalidate_collection("/palace/a", "col1")
        time.sleep(0.001)

        assert cache.get_value(key_col1) is None
        assert cache.get_value(key_col2) == {"col": "col2"}  # unaffected

    def test_invalidate_collection_no_cross_palace_contamination(self):
        """Write to palace A does NOT evict entries for palace B."""
        cache = QueryCache(ttl_seconds=60)

        key_a = "/palace/a|default|query|None|None|None|None|5|False|None|None"
        key_b = "/palace/b|default|query|None|None|None|None|5|False|None|None"
        cache.set_value(key_a, {"data": "A"})
        cache.set_value(key_b, {"data": "B"})

        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        assert cache.get_value(key_a) is None
        assert cache.get_value(key_b) == {"data": "B"}  # B untouched


class TestClearVsInvalidateCollection:
    """clear() vs invalidate_collection() distinction."""

    def test_clear_removes_all_entries(self):
        """clear() removes every entry regardless of palace/collection."""
        cache = QueryCache(ttl_seconds=60)
        cache.set("/palace/a", "default", ["q"], 5, {"a": True})
        cache.set_value("/palace/b|default|q|None|None|None|None|5|False|None|None", {"b": True})

        cache.clear()

        assert cache.get("/palace/a", "default", ["q"], 5) is None
        assert cache.get_value("/palace/b|default|q|None|None|None|None|5|False|None|None") is None

    def test_invalidate_collection_only_affects_target_palace(self):
        """invalidate_collection is selective; clear() is global."""
        cache = QueryCache(ttl_seconds=60)
        cache.set("/palace/a", "default", ["q"], 5, {"a": True})
        cache.set("/palace/b", "default", ["q"], 5, {"b": True})

        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        assert cache.get("/palace/a", "default", ["q"], 5) is None
        assert cache.get("/palace/b", "default", ["q"], 5) == {"b": True}  # untouched


class TestStalenessAfterWrite:
    """Search cache must not return stale results after writes."""

    def test_invalidate_query_cache_clears_all_search_memories_entries(self):
        """invalidate_query_cache() → clear() removes all search_memories entries."""
        cache = get_query_cache()

        keys = [
            "/palace/a|default|q1|None|None|None|None|5|False|None|None",
            "/palace/b|default|q2|None|None|None|None|5|False|None|None",
        ]
        for k in keys:
            cache.set_value(k, {"results": k})

        invalidate_query_cache()

        for k in keys:
            assert cache.get_value(k) is None, f"{k} should be cleared"

    def test_invalidate_all_caches_alias_works(self):
        """invalidate_all_caches is an alias for invalidate_query_cache."""
        cache = get_query_cache()
        key = "/palace/test|default|q|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"data": "fresh"})

        invalidate_all_caches()

        assert cache.get_value(key) is None

    def test_raw_key_staleness_prevented_by_invalidate_collection(self):
        """After invalidate_collection, raw-key entries for that palace are gone."""
        cache = QueryCache(ttl_seconds=60)
        key = "/palace/a|default|error|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"results": ["old", "stale"]})

        # Simulate write → invalidate_collection (Lance write path)
        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        # Next search_memories call will miss cache and hit backend
        assert cache.get_value(key) is None

    def test_concurrent_invalidate_and_get_value(self):
        """Concurrent invalidate_collection + get_value must not raise."""
        cache = QueryCache(ttl_seconds=60)
        for i in range(50):
            key = f"/palace/a|default|q{i}|None|None|None|None|5|False|None|None"
            cache.set_value(key, {"i": i})

        errors = []

        def inval():
            for _ in range(50):
                cache.invalidate_collection("/palace/a", "default")
                time.sleep(0.001)

        def getter(i):
            try:
                key = f"/palace/a|default|q{i}|None|None|None|None|5|False|None|None"
                cache.get_value(key)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=inval)]
        threads.extend([threading.Thread(target=lambda i=i: getter(i)) for i in range(10)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestCrossPalaceIsolation:
    """Different palaces must not share cache entries."""

    def test_different_palace_paths_different_keys(self):
        """Same query, different palace → different cache keys."""
        def make_key(p, c, q):
            return f"{p}|{c}|{q}|None|None|None|None|5|False|None|None"

        key_a = make_key("/palace/a", "default", "error")
        key_b = make_key("/palace/b", "default", "error")

        assert key_a != key_b
        assert "/palace/a" in key_a
        assert "/palace/b" in key_b

    def test_write_to_one_palace_preserves_other(self):
        """Write to palace A preserves cache for palace B."""
        cache = QueryCache(ttl_seconds=60)

        key_a = "/palace/a|default|error|None|None|None|None|5|False|None|None"
        key_b = "/palace/b|default|error|None|None|None|None|5|False|None|None"
        cache.set_value(key_a, {"result": "A"})
        cache.set_value(key_b, {"result": "B"})

        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        assert cache.get_value(key_a) is None
        assert cache.get_value(key_b) == {"result": "B"}

    def test_same_palace_different_collection_isolation(self):
        """Same palace, different collections are isolated."""
        cache = QueryCache(ttl_seconds=60)

        key_default = "/palace|default|query|None|None|None|None|5|False|None|None"
        key_custom = "/palace|custom|query|None|None|None|None|5|False|None|None"
        cache.set_value(key_default, {"col": "default"})
        cache.set_value(key_custom, {"col": "custom"})

        cache.invalidate_collection("/palace", "default")
        time.sleep(0.001)

        assert cache.get_value(key_default) is None
        assert cache.get_value(key_custom) == {"col": "custom"}


class TestInvalidateQueryCacheContract:
    """Contract for invalidate_query_cache() — the write-path invalidation."""

    def test_invalidate_query_cache_calls_clear(self):
        """invalidate_query_cache() removes all entries from the singleton cache."""
        cache = get_query_cache()

        # Pre-populate
        for palace in ["/palace/a", "/palace/b"]:
            for i in range(3):
                key = f"{palace}|default|q{i}|None|None|None|None|5|False|None|None"
                cache.set_value(key, {"palace": palace, "i": i})

        initial_entries = cache._total_size()
        assert initial_entries > 0

        invalidate_query_cache()

        # All entries gone
        assert cache._total_size() == 0

    def test_write_tool_chain_full_clear(self):
        """Simulate full write tool chain: Lance write → invalidate_query_cache."""
        cache = get_query_cache()
        key = "/tmp/palace|default|error|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"results": ["old"]})

        # Simulate: lance.upsert() → invalidate_collection() → write_tools invalidate_query_cache()
        # After both: cache must be empty
        cache.invalidate_collection("/tmp/palace", "default")
        cache.clear()  # the write_tools path

        assert cache.get_value(key) is None

    def test_invalidate_collection_alone_is_sufficient(self):
        """With the fix, invalidate_collection() alone clears all entries for that palace.

        Both structured (get/set) and raw (get_value/set_value) entries are removed.
        """
        cache = QueryCache(ttl_seconds=60)
        structured_key = ("/palace/a", "default", ["q"], 5)
        raw_key = "/palace/a|default|q|None|None|None|None|5|False|None|None"

        cache.set(*structured_key, {"structured": True})
        cache.set_value(raw_key, {"raw": True})

        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        assert cache.get(*structured_key) is None
        assert cache.get_value(raw_key) is None
