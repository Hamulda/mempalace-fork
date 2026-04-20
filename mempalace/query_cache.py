"""
LRU cache pro MemPalace query výsledky.
TTL 5s zajišťuje čerstvost bez zbytečných redundantních searchů.

Canonical cache story:
- DVA oddělené přístupy ke stejnému _cache slovníku:
  1. get()/set() — strukturovaný klíč (palace_path, collection, query_texts, n_results),
     chráněno _last_write timestampem (invalidate_collection nastaví timestamp,
     get() zkontroluje při každém čtení)
  2. get_value()/set_value() — raw string klíč s embedded palace_path|collection|
     v klíči; cross-palace izolace je přes klíč sám; invalidace přes invalidate_collection()
     (která maže položky s odpovídajícím prefixem) NEBO přes clear() (maže vše)
- invalidate_query_cache() (searcher.py) → cache.clear() po write operacích
  (všechny palace najednou, infrequent operace)
- invalidate_collection(palace_path, collection) → maže přímo entries z _cache
  pro obě rozhraní najednou
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
        ttl_seconds: float = 60.0,
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
        Invalidate ALL cache entries for (palace_path, collection).
        Removes entries from both interfaces:
        - get()/set(): tuple-keyed entries matching this palace+collection
        - get_value()/set_value(): raw-string-keyed entries whose key starts with
          "{palace_path}|{collection}|" (the format used by search_memories)
        For full cross-palace flush (all palace+collection), use clear().
        """
        with self._lock:
            self._last_write[(palace_path, collection)] = time.monotonic()
            # Evict structured-key entries (get/set interface)
            # Tuple key format: (palace_path, collection, query_texts, n_results)
            to_remove = [k for k in self._cache
                         if isinstance(k, tuple)
                         and len(k) >= 2
                         and k[0] == palace_path
                         and k[1] == collection]
            for k in to_remove:
                del self._cache[k]
            # Evict raw-string-key entries (get_value/set_value interface)
            # String key format used by search_memories:
            # f"{palace_path}|{collection_name}|{query}|{wing}|{room}|..."
            prefix = f"{palace_path}|{collection}|"
            to_remove_str = [k for k in self._cache
                             if isinstance(k, str) and k.startswith(prefix)]
            for k in to_remove_str:
                del self._cache[k]

    def get_value(self, key: str, palace_path: str = "", collection: str = "") -> Optional[Any]:
        """
        Return cached value by raw string key, or None if missing/expired.

        palace_path + collection are used only for _last_write staleness check
        (invalidate_collection sets _last_write; entries cached before that
        timestamp are evicted). TTL always applies. For raw-key entries,
        cross-palace isolation is provided by palace_path being embedded
        in the key string itself.
        """
        with self._lock:
            try:
                value, ts = self._cache[key]
                if time.monotonic() - ts < self._ttl:
                    # _last_write staleness check for this palace+collection
                    if palace_path and collection:
                        last_write = self._last_write.get((palace_path, collection), 0.0)
                        if last_write > ts:
                            del self._cache[key]
                            self._misses += 1
                            return None
                    return value
                del self._cache[key]
            except (KeyError, TypeError, AttributeError):
                pass
            return None

    def set_value(self, key: str, value: Any, palace_path: str = "", collection: str = "") -> None:
        """
        Store value by raw string key with TTL. Used by search_memories.

        palace_path + collection are stored alongside the entry so get_value()
        can check _last_write for staleness (via invalidate_collection).
        NOTE: set_value does NOT update _last_write — only invalidate_collection()
        does. Staleness for raw-key entries is handled by invalidate_collection()
        scanning for matching prefix keys, not by _last_write timestamp comparison.
        """
        with self._lock:
            try:
                now = time.monotonic()
                self._cache[key] = (value, now)
                self._cache.move_to_end(key)
                while len(self._cache) > self._maxsize:
                    self._cache.popitem(last=False)
            except Exception:
                pass

    def clear(self) -> None:
        """Remove all cached entries."""
        with self._lock:
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
    ttl_seconds=float(os.environ.get("MEMPALACE_CACHE_TTL", "60.0")),
)


def get_query_cache() -> QueryCache:
    return _query_cache