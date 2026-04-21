"""
LRU cache pro MemPalace query výsledky.
TTL 5s zajišťuje čerstvost bez zbytečných redundantních searchů.

Canonical cache story:
- DVA oddělené přástupy ke stejnému _cache slovníku:
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
from __future__ import annotations

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

    Sharded locking: 8 shards reduce read/write contention under concurrent
    use. Each shard has its own lock and LRU dict — concurrent sessions hitting
    different palace+collection combos proceed in parallel.
    """

    _NUM_SHARDS = 8

    def __init__(
        self,
        maxsize: int = 256,
        ttl_seconds: float = 60.0,
    ):
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0
        # Per-shard data structures — each shard is fully independent
        self._shards: list[dict] = [
            {
                "cache": OrderedDict[str, tuple[Any, float]](),
                # Per-(palace_path, collection) write timestamps for cross-palace isolation.
                "last_write": {},  # Key: (palace_path, collection_name)
            }
            for _ in range(self._NUM_SHARDS)
        ]
        # One lock per shard — concurrent access to different shards is parallel
        self._shard_locks = [threading.Lock() for _ in range(self._NUM_SHARDS)]
        self._global_lock = threading.Lock()  # Only for stats() access

    def _shard_idx(self, palace_path: str, collection: str) -> int:
        """Select shard from palace_path + collection (stable, no hash randomization)."""
        key = f"{palace_path or ''}|{collection or ''}"
        return hash(key) % self._NUM_SHARDS

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
        shard_idx = self._shard_idx(palace_path, collection)
        lock = self._shard_locks[shard_idx]
        shard = self._shards[shard_idx]

        with lock:
            cache = shard["cache"]
            last_write = shard["last_write"]

            if key not in cache:
                with self._global_lock:
                    self._misses += 1
                return None

            result, ts = cache[key]

            # Zkontroluj TTL
            if now - ts > self._ttl:
                del cache[key]
                with self._global_lock:
                    self._misses += 1
                return None

            # Zkontroluj jestli nebyl write po uložení do cache
            last_write_ts = last_write.get((palace_path, collection), 0.0)
            if last_write_ts > ts:
                del cache[key]
                with self._global_lock:
                    self._misses += 1
                return None

            # Cache hit – přesuň na konec (LRU update)
            cache.move_to_end(key)
            with self._global_lock:
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
        shard_idx = self._shard_idx(palace_path, collection)
        lock = self._shard_locks[shard_idx]
        shard = self._shards[shard_idx]

        with lock:
            cache = shard["cache"]
            cache[key] = (result, now)
            cache.move_to_end(key)

            # Evict nejstarší položky pokud překračujeme maxsize (per-shard)
            while len(cache) > self._maxsize:
                cache.popitem(last=False)

    def invalidate_collection(self, palace_path: str, collection: str) -> None:
        """
        Invalidate ALL cache entries for (palace_path, collection) across ALL shards.
        Removes entries from both interfaces:
        - get()/set(): tuple-keyed entries matching this palace+collection
        - get_value()/set_value(): raw-string-keyed entries whose key starts with
          "{palace_path}|{collection}|" (the format used by search_memories)
        For full cross-palace flush (all palace+collection), use clear().
        """
        prefix = f"{palace_path}|{collection}|"
        target_shard = self._shard_idx(palace_path, collection)
        for i in range(self._NUM_SHARDS):
            lock = self._shard_locks[i]
            with lock:
                shard = self._shards[i]
                cache = shard["cache"]
                if i == target_shard:
                    shard["last_write"][(palace_path, collection)] = time.monotonic()
                # Evict raw-string-key entries with matching prefix
                for k in list(cache.keys()):
                    if isinstance(k, str) and k.startswith(prefix):
                        cache.pop(k, None)

    def get_value(self, key: str, palace_path: str = "", collection: str = "") -> Optional[Any]:
        """
        Return cached value by raw string key, or None if missing/expired.

        palace_path + collection are used only for _last_write staleness check
        (invalidate_collection sets _last_write; entries cached before that
        timestamp are evicted). TTL always applies. For raw-key entries,
        cross-palace isolation is provided by palace_path being embedded
        in the key string itself.
        """
        # Extract palace_path+collection from the key itself when not provided.
        # This is critical for cross-shard correctness: the key format is
        # "{palace_path}|{collection}|..." so we can parse it.
        if palace_path and collection:
            shard_idx = self._shard_idx(palace_path, collection)
        else:
            # Parse palace_path+collection from the key for correct shard routing
            parts = key.split("|", 2)
            if len(parts) >= 2:
                palace_path, collection = parts[0], parts[1]
            shard_idx = self._shard_idx(palace_path or "", collection or "")
        lock = self._shard_locks[shard_idx]
        shard = self._shards[shard_idx]

        with lock:
            try:
                cache = shard["cache"]
                value, ts = cache[key]
                if time.monotonic() - ts < self._ttl:
                    # _last_write staleness check for this palace+collection
                    if palace_path and collection:
                        last_write_ts = shard["last_write"].get((palace_path, collection), 0.0)
                        if last_write_ts > ts:
                            del cache[key]
                            with self._global_lock:
                                self._misses += 1
                            return None
                    return value
                del cache[key]
            except (KeyError, TypeError, AttributeError):
                pass
            with self._global_lock:
                self._misses += 1
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
        # Extract palace_path+collection from the key itself when not provided.
        if palace_path and collection:
            shard_idx = self._shard_idx(palace_path, collection)
        else:
            parts = key.split("|", 2)
            if len(parts) >= 2:
                palace_path, collection = parts[0], parts[1]
            shard_idx = self._shard_idx(palace_path or "", collection or "")
        lock = self._shard_locks[shard_idx]
        shard = self._shards[shard_idx]

        with lock:
            try:
                now = time.monotonic()
                cache = shard["cache"]
                cache[key] = (value, now)
                cache.move_to_end(key)
                while len(cache) > self._maxsize:
                    cache.popitem(last=False)
            except Exception:
                pass

    def clear(self) -> None:
        """Remove all cached entries across all shards."""
        for i in range(self._NUM_SHARDS):
            with self._shard_locks[i]:
                try:
                    self._shards[i]["cache"].clear()
                    self._shards[i]["last_write"].clear()
                except Exception:
                    pass

    def stats(self) -> dict:
        with self._global_lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            cached = sum(len(s["cache"]) for s in self._shards)
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1%}",
            "cached_entries": cached,
            "shards": self._NUM_SHARDS,
        }

    def _total_size(self) -> int:
        """Total entries across all shards. For test compatibility."""
        return sum(len(s["cache"]) for s in self._shards)

    def _all_keys(self) -> set[str]:
        """All keys across all shards. For test compatibility."""
        result = set()
        for shard in self._shards:
            result.update(shard["cache"].keys())
        return result


# Globální cache singleton sdílená v HTTP MCP serveru
_query_cache = QueryCache(
    maxsize=int(os.environ.get("MEMPALACE_CACHE_SIZE", "256")),
    ttl_seconds=float(os.environ.get("MEMPALACE_CACHE_TTL", "60.0")),
)


def get_query_cache() -> QueryCache:
    return _query_cache