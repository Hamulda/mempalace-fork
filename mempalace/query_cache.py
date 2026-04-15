"""
LRU cache pro MemPalace query výsledky.
TTL 5s zajišťuje čerstvost bez zbytečných redundantních searchů.

Canonical cache story:
- search_memories používá get_value/set_value s klíčem
  "{palace_path}|{collection_name}|{query}|{filters}..."
  (palace_path je vždy součástí klíče → cross-palace izolace)
- get/set (pro interní LanceBackend query caching) používá
  palace_path + collection_name pro správnou cross-palace invalidaci
- invalidate_collection(palace_path, collection_name) zapisuje timestamp
  do _last_write[(palace_path, collection_name)] — tento timestamp
  pak zneplatní get/set cache entries i get_value/set_value entries
  (protože search_memories volá invalidate_query_cache → clear())
- write invalidace: LanceDB add/upsert/delete volá
  get_query_cache().invalidate_collection(palace_path, collection_name)
  → fastmcp_server volá invalidate_all_caches() = clear() pro search cache
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

    Cross-palace izolace: _last_write je indexovaný (palace_path, collection_name),
    takže write do jednoho palace nikdy nezneplatní cache jiného palace
    (pokud caller nevolá clear()).

    Cache key pro get/set: hash(collection + query_texts + n_results)
    Cache key pro get_value/set_value: libovolný string (caller definuje format)
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
        # Per-(palace_path, collection) write timestamps for cross-palace isolation.
        # Key format: (palace_path, collection_name) — palace_path "" means global.
        self._last_write: dict[tuple[str, str], float] = {}

    def _make_key(
        self, palace_path: str, collection: str, query_texts: list[str], n_results: int
    ) -> str:
        raw = json.dumps({
            "p": palace_path,
            "c": collection,
            "q": query_texts,
            "n": n_results,
        }, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(
        self,
        palace_path: str,
        collection: str,
        query_texts: list[str],
        n_results: int,
    ) -> Optional[Any]:
        """Vrátí cached výsledek nebo None pokud cache miss/expired."""
        key = self._make_key(palace_path, collection, query_texts, n_results)
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
            # Use (palace_path, collection) composite key for cross-palace accuracy
            last_write = self._last_write.get((palace_path, collection), 0.0)
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
        palace_path: str,
        collection: str,
        query_texts: list[str],
        n_results: int,
        result: Any,
    ) -> None:
        """Uloží výsledek do cache."""
        key = self._make_key(palace_path, collection, query_texts, n_results)
        now = time.monotonic()

        with self._lock:
            self._cache[key] = (result, now)
            self._cache.move_to_end(key)

            # Evict nejstarší položky pokud překračujeme maxsize
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def invalidate_collection(self, palace_path: str, collection: str) -> None:
        """
        Zaznamenej write event pro (palace_path, collection) pár.
        Nezahazuj cache hned – TTL to vyřeší lazily.
        Cross-palace izolace: jen tento konkrétní palace+collection je označen.
        Pro hromadnou invalidaci (všechny palace) použij clear().
        """
        with self._lock:
            self._last_write[(palace_path, collection)] = time.monotonic()

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

    def clear(self) -> None:
        """Remove all cached entries."""
        try:
            self._cache.clear()
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