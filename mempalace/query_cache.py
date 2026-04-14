"""
LRU cache pro MemPalace query výsledky.
TTL 5s zajišťuje čerstvost bez zbytečných redundantních searchů.
"""
import time
import threading
from collections import OrderedDict
from typing import Any, Optional
import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)


class QueryCache:
    """
    Thread-safe LRU cache s TTL pro query výsledky.

    Klíč: (query_text, n_results, collection_name)
    Hodnota: (result_dict, timestamp)
    Invalidace: při každém write do stejné collection
    """

    def __init__(
        self,
        maxsize: int = 256,
        ttl_seconds: float = 5.0,
    ):
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        # Per-collection write timestamps pro invalidaci
        self._last_write: dict[str, float] = {}

    def _make_key(
        self, collection: str, query_texts: list[str], n_results: int
    ) -> str:
        raw = json.dumps({
            "c": collection,
            "q": query_texts,
            "n": n_results,
        }, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(
        self,
        collection: str,
        query_texts: list[str],
        n_results: int,
    ) -> Optional[Any]:
        """Vrátí cached výsledek nebo None pokud cache miss/expired."""
        key = self._make_key(collection, query_texts, n_results)
        now = time.monotonic()

        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            result, ts = self._cache[key]

            # Zkontroluj TTL
            if now - ts > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None

            # Zkontroluj jestli nebyl write po uložení do cache
            last_write = self._last_write.get(collection, 0.0)
            if last_write > ts:
                del self._cache[key]
                self._misses += 1
                return None

            # Cache hit – přesuň na konec (LRU update)
            self._cache.move_to_end(key)
            self._hits += 1
            return result

    def set(
        self,
        collection: str,
        query_texts: list[str],
        n_results: int,
        result: Any,
    ) -> None:
        """Uloží výsledek do cache."""
        key = self._make_key(collection, query_texts, n_results)
        now = time.monotonic()

        with self._lock:
            self._cache[key] = (result, now)
            self._cache.move_to_end(key)

            # Evict nejstarší položky pokud překračujeme maxsize
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def invalidate_collection(self, collection: str) -> None:
        """
        Zaznamenej write event pro collection.
        Nezahazuj cache hned – TTL to vyřeší lazily.
        """
        with self._lock:
            self._last_write[collection] = time.monotonic()

    def get_value(self, key: str) -> Optional[Any]:
        """Return cached value by key string, or None if missing/expired. Used by search_memories."""
        try:
            value, ts = self._cache[key]
            if time.monotonic() - ts < self._ttl:
                return value
            del self._cache[key]
        except (KeyError, TypeError, AttributeError):
            pass
        return None

    def set_value(self, key: str, value: Any) -> None:
        """Store value by key string with TTL. Used by search_memories."""
        try:
            self._cache[key] = (value, time.monotonic())
            self._cache.move_to_end(key)
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)
        except Exception:
            pass

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{hit_rate:.1%}",
                "cached_entries": len(self._cache),
            }


# Globální cache singleton sdílená v HTTP MCP serveru
_query_cache = QueryCache(
    maxsize=int(os.environ.get("MEMPALACE_CACHE_SIZE", "256")),
    ttl_seconds=float(os.environ.get("MEMPALACE_CACHE_TTL", "5.0")),
)


def get_query_cache() -> QueryCache:
    return _query_cache