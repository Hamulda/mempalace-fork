"""
Tests for QueryCache.

Run: pytest tests/test_query_cache.py -v -s
"""

import time
import pytest
from unittest import mock

from mempalace.query_cache import QueryCache


class TestQueryCache:
    def test_cache_hit_on_repeat_query(self):
        """Druhý identický query vrátí cache hit."""
        cache = QueryCache(ttl_seconds=10)
        result = {"ids": [["1"]], "documents": [["test"]]}
        cache.set("/palace", "col1", ["query"], 5, result)

        cached = cache.get("/palace", "col1", ["query"], 5)
        assert cached == result
        assert cache.stats()["hits"] == 1

    def test_cache_miss_after_write(self):
        """Cache miss po write do stejné collection."""
        cache = QueryCache(ttl_seconds=60)
        cache.set("/palace", "col1", ["query"], 5, {"ids": [[]]})
        cache.invalidate_collection("/palace", "col1")
        time.sleep(0.001)  # zajisti že write timestamp > cache timestamp

        result = cache.get("/palace", "col1", ["query"], 5)
        assert result is None

    def test_cache_miss_after_ttl(self):
        """Cache miss po vypršení TTL."""
        cache = QueryCache(ttl_seconds=0.05)
        cache.set("/palace", "col1", ["query"], 5, {"ids": [[]]})
        time.sleep(0.1)

        assert cache.get("/palace", "col1", ["query"], 5) is None

    def test_lru_eviction(self):
        """LRU evikce při překročení maxsize."""
        cache = QueryCache(maxsize=3, ttl_seconds=60)
        for i in range(4):
            cache.set("/palace", "col", [f"q{i}"], 5, {"n": i})

        assert len(cache._cache) == 3
        assert cache.get("/palace", "col", ["q0"], 5) is None  # nejstarší evictnut

    def test_different_queries_not_cached(self):
        """Různé query texty dávají různé cache entries."""
        cache = QueryCache(ttl_seconds=60)
        cache.set("/palace", "col1", ["query1"], 5, {"result": 1})
        cache.set("/palace", "col1", ["query2"], 5, {"result": 2})

        assert cache.get("/palace", "col1", ["query1"], 5) == {"result": 1}
        assert cache.get("/palace", "col1", ["query2"], 5) == {"result": 2}

    def test_stats_tracking(self):
        """Stats správně počítají hits/misses."""
        cache = QueryCache(ttl_seconds=10)

        # Miss
        assert cache.get("/palace", "col", ["q"], 5) is None
        # Hit
        cache.set("/palace", "col", ["q"], 5, {"data": "value"})
        assert cache.get("/palace", "col", ["q"], 5) == {"data": "value"}

        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_cross_palace_isolation(self):
        """Write do palace A nezneplatní cache palace B."""
        cache = QueryCache(ttl_seconds=60)
        cache.set("/palace/a", "col", ["query"], 5, {"result": "A"})
        cache.set("/palace/b", "col", ["query"], 5, {"result": "B"})

        # Write do palace A
        cache.invalidate_collection("/palace/a", "col")
        time.sleep(0.001)

        # Palace A cache je zneplatněna
        assert cache.get("/palace/a", "col", ["query"], 5) is None
        # Palace B cache zůstává
        assert cache.get("/palace/b", "col", ["query"], 5) == {"result": "B"}