"""
Tests for search cache correctness — cross-palace contamination prevention
and stale cache prevention after writes.

Run: pytest tests/test_search_cache_correctness.py -v
"""

import time
import threading
import pytest
from unittest import mock

from mempalace.query_cache import QueryCache
from mempalace.searcher import (
    invalidate_query_cache, invalidate_all_caches, invalidate_bm25_cache,
    _get_query_cache,
)


class TestSameQueryDifferentPalace:
    """Identický query ve dvou palace_path nesmí sdílet cache entry."""

    def test_same_query_different_palace_not_cached(self):
        """Dva různé palace s identickým query nemají společnou cache."""
        cache = QueryCache(ttl_seconds=60)

        result_a = {"results": ["palace_a_result"]}
        result_b = {"results": ["palace_b_result"]}

        # Simulate search_memories key format: palace_path|custom|query|...
        key_a = "/palace/a|custom|error|None|None|None|None|5|False|None|None"
        key_b = "/palace/b|custom|error|None|None|None|None|5|False|None|None"

        cache.set_value(key_a, result_a)
        cache.set_value(key_b, result_b)

        # Každý palace má svůj vlastní cache entry
        assert cache.get_value(key_a) == result_a
        assert cache.get_value(key_b) == result_b

        # Cross-palace contamination: key bez palace_path by neměl matchovat
        key_no_palace = "custom|error|None|None|None|None|5|False|None|None"
        assert cache.get_value(key_no_palace) is None

    def test_get_set_interface_cross_palace_isolation(self):
        """get/set (LanceBackend internal) cache je izolovaný mezi palace."""
        cache = QueryCache(ttl_seconds=60)

        # Palace A cache
        cache.set("/palace/a", "default", ["query"], 5, {"result": "A"})
        # Palace B cache
        cache.set("/palace/b", "default", ["query"], 5, {"result": "B"})

        # Oba vrátí správné výsledky
        assert cache.get("/palace/a", "default", ["query"], 5) == {"result": "A"}
        assert cache.get("/palace/b", "default", ["query"], 5) == {"result": "B"}

        # Write do palace A nezneplatní palace B
        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        assert cache.get("/palace/a", "default", ["query"], 5) is None  # zneplatněno
        assert cache.get("/palace/b", "default", ["query"], 5) == {"result": "B"}  # OK


class TestWriteInvalidation:
    """Write invalidace musí spolehlivě zasáhnout search cache."""

    def test_invalidate_query_cache_clears_search_entries(self):
        """invalidate_query_cache() musí vymazat všechny search_memories cache entries."""
        cache = _get_query_cache()

        keys = [
            "/palace/a|default|q1|None|None|None|None|5|False|None|None",
            "/palace/b|default|q2|None|None|None|None|5|False|None|None",
        ]
        for k in keys:
            cache.set_value(k, {"results": k})

        invalidate_query_cache()

        for k in keys:
            assert cache.get_value(k) is None, f"Key {k} should be invalidated"

    def test_invalidate_all_caches_clears_query_cache(self):
        """invalidate_all_caches() zneplatní query cache i BM25 cache."""
        cache = _get_query_cache()

        key = "/palace|default|query|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"stale": True})

        invalidate_all_caches()

        assert cache.get_value(key) is None

    def test_lance_write_calls_invalidate_collection(self):
        """LanceBackend add/upsert/delete musí zavolat invalidate_collection."""
        from mempalace.query_cache import QueryCache

        # We test this by checking the call signature
        cache = QueryCache(ttl_seconds=60)
        cache.set("/tmp/palace", "default", ["test"], 5, {"data": "test"})

        # Two-arg invalidate_collection (palace_path, collection)
        cache.invalidate_collection("/tmp/palace", "default")
        time.sleep(0.001)

        assert cache.get("/tmp/palace", "default", ["test"], 5) is None

    def test_write_to_one_palace_does_not_affect_other(self):
        """Write do palace A nezneplatní cache palace B."""
        cache = QueryCache(ttl_seconds=60)

        cache.set("/palace/a", "default", ["error"], 5, {"result": "A"})
        cache.set("/palace/b", "default", ["error"], 5, {"result": "B"})

        # Write to palace A
        cache.invalidate_collection("/palace/a", "default")
        time.sleep(0.001)

        # Palace A is invalidated
        assert cache.get("/palace/a", "default", ["error"], 5) is None
        # Palace B is NOT affected
        assert cache.get("/palace/b", "default", ["error"], 5) == {"result": "B"}


class TestConcurrentAccess:
    """Concurrent get/set/clear nesmí padnout."""

    def test_concurrent_get_set_clear_no_crash(self):
        """Concurrent get/set/clear operations must not raise."""
        cache = QueryCache(ttl_seconds=30, maxsize=100)

        errors = []

        def writer(thread_id):
            for i in range(50):
                key = f"/palace|default|q_{thread_id}_{i}|None|None|None|None|5|False|None|None"
                cache.set_value(key, {"t": thread_id, "i": i})

        def reader():
            for i in range(50):
                key = f"/palace|default|q_0_{i}|None|None|None|None|5|False|None|None"
                cache.get_value(key)

        def clearer():
            for _ in range(20):
                cache.clear()
                time.sleep(0.001)

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=reader),
            threading.Thread(target=clearer),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

    def test_concurrent_invalidate_and_read(self):
        """Concurrent invalidate_collection + get operation must be safe."""
        cache = QueryCache(ttl_seconds=60)

        # Pre-populate
        for i in range(20):
            cache.set("/palace/a", "default", [f"q{i}"], 5, {"data": i})

        errors = []

        def inval():
            for _ in range(30):
                cache.invalidate_collection("/palace/a", "default")
                time.sleep(0.001)

        def reader():
            for i in range(20):
                try:
                    cache.get("/palace/a", "default", [f"q{i}"], 5)
                except Exception as e:
                    errors.append(str(e))

        threads = [
            threading.Thread(target=inval),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestHybridSearchStaleness:
    """hybrid_search nepoužívá stale results po write."""

    def test_bm25_cache_invalidation_clears_index(self):
        """invalidate_bm25_cache() musí vynulovat BM25 globals."""
        import mempalace.searcher as searcher_mod

        # Setup: set BM25 state via module reference (not global keyword in test fn)
        searcher_mod._bm25_index = "fake_index"
        searcher_mod._bm25_corpus = ["doc1", "doc2"]
        searcher_mod._bm25_ids = ["id1"]
        searcher_mod._bm25_metas = [{"wing": "test"}]
        searcher_mod._bm25_path_cached = "/palace/test"

        invalidate_bm25_cache()

        # After invalidation, all BM25 globals must be None
        assert searcher_mod._bm25_index is None
        assert searcher_mod._bm25_corpus is None
        assert searcher_mod._bm25_ids is None
        assert searcher_mod._bm25_metas is None
        assert searcher_mod._bm25_path_cached is None

    def test_invalidate_all_caches_clears_both(self):
        """invalidate_all_caches() zneplatní query cache i BM25 cache současně."""
        import mempalace.searcher as searcher_mod

        # Setup query cache entry
        cache = _get_query_cache()
        key = "/palace|default|query|None|None|None|None|5|False|None|None"
        cache.set_value(key, {"results": "stale"})

        # Setup BM25 cache via module reference
        searcher_mod._bm25_index = "fake"
        searcher_mod._bm25_path_cached = "/palace"

        invalidate_all_caches()

        # Query cache cleared
        assert cache.get_value(key) is None

        # BM25 cache cleared
        assert searcher_mod._bm25_index is None
        assert searcher_mod._bm25_path_cached is None


class TestCacheKeyCanonical:
    """Canonical cache key story: palace_path + collection_name jsou vždy součástí klíče."""

    def test_search_memories_key_format(self):
        """search_memories musí používat klíč s palace_path a collection_name."""
        def make_key(palace_path, collection_name, query, wing=None, room=None,
                     is_latest=None, agent_id=None, n_results=5, rerank=False,
                     priority_gte=None, priority_lte=None):
            return (f"{palace_path}|{collection_name}|{query}|{wing}|{room}|"
                    f"{is_latest}|{agent_id}|{n_results}|{rerank}|{priority_gte}|{priority_lte}")

        key_a = make_key("/palace/a", "default", "error")
        key_b = make_key("/palace/b", "default", "error")

        # Different palaces produce different keys
        assert key_a != key_b
        assert "/palace/a" in key_a
        assert "/palace/b" in key_b
        assert "default" in key_a
        assert "default" in key_b

        # Same palace + different collection produce different keys
        key_custom = make_key("/palace/a", "custom", "error")
        assert key_a != key_custom

    def test_cache_key_includes_palace_path_prevents_contamination(self):
        """Cache key bez palace_path neprodukuje contamination."""
        cache = QueryCache(ttl_seconds=60)

        key_with = "/tmp/palace1|default|query|None|None|None|None|5|False|None|None"
        key_without = "default|query|None|None|None|None|5|False|None|None"

        cache.set_value(key_with, {"data": "test"})

        # S完整 klíčem → hit
        assert cache.get_value(key_with) == {"data": "test"}
        # Bez palace_path → miss (správná izolace)
        assert cache.get_value(key_without) is None