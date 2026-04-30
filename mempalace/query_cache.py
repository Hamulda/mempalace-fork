"""
LRU cache pro MemPalace query výsledky.
TTL 60s (výchozí) zajišťuje čerstvost bez zbytečných redundantních searchů.

8-shard architektura: kazdy shard má vlastni cache + _last_write mapu.
Cross-palace izolace je dana palace_path v cache key (get/set) nebo embedding
v raw-string key (get_value/set_value).

Dva oddělene přístupy ke stejnému _shards slovníku:
  1. get()/set() — strukturovaný klíč (palace_path, collection, query_texts, n_results),
     chráněno _last_write timestampem (invalidate_collection nastaví timestamp,
     get() zkontroluje při každém čtení)
  2. get_value()/set_value() — raw string klíč s embedded palace_path|collection|
     v klíči; cross-palace izolace je přes klíč sám; invalidace přes invalidate_collection()
     (která maže položky s odpovídajícím prefixem) NEBO přes clear() (maže vše)

Canonical invalidation story:
  - invalidate_query_cache() (searcher.py) → cache.clear() — full cross-palace flush
  - invalidate_collection(palace_path, collection) → maže entries PRES PREFIX KEY SCAN
    (nikol přes _last_write timestamp pro raw-key entries)
  - _last_write timestamp se používá jen v get()/set() rozhraní
"""
from __future__ import annotations

from collections import OrderedDict
import time
import threading
from typing import Any
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

    def get(
        self,
        palace_path: str,
        collection: str,
        query_texts: list[str],
        n_results: int,
    ) -> Any | None:
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
            val = cache.pop(key)
            cache[key] = val
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
            # Pop and re-insert for LRU ordering
            if key in cache:
                del cache[key]
            cache[key] = (result, now)

            # Evict nejstarší položky pokud překračujeme maxsize (per-shard)
            while len(cache) > self._maxsize:
                oldest = next(iter(cache))
                del cache[oldest]

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

    def get_value(self, key: str, palace_path: str = "", collection: str = "") -> Any | None:
        """
        Return cached value by raw string key, or None if missing/expired.

        palace_path + collection are used only for routing to the correct shard
        and (when provided) for _last_write staleness check via get()/set() interface.
        TTL always applies.

        For raw-key entries, cross-palace isolation is provided by palace_path being
        embedded in the key string itself (format: "{palace_path}|{collection}|...").

        NOTE: set_value does NOT update _last_write — only invalidate_collection()
        does. For raw-key entries, staleness is handled by invalidate_collection()
        scanning for matching prefix keys, NOT by _last_write timestamp comparison.
        The _last_write staleness check below is only effective for entries stored
        via get()/set() (structured key interface).
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
                # TTL expiry check before insert — symmetrical with get_value
                if key in cache:
                    _, ts = cache[key]
                    if now - ts >= self._ttl:
                        del cache[key]
                cache[key] = (value, now)
                while len(cache) > self._maxsize:
                    oldest = next(iter(cache))
                    del cache[oldest]
            except Exception:
                logger.warning("query_cache.set_value failed: shard=%d key=%s", shard_idx, key)

    def clear(self) -> None:
        """Remove all cached entries and all _last_write timestamps across all shards."""
        for i in range(self._NUM_SHARDS):
            with self._shard_locks[i]:
                try:
                    self._shards[i]["cache"].clear()
                    self._shards[i]["last_write"].clear()
                except Exception:
                    logger.warning("query_cache.clear failed: shard=%d", i)

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


class EmbeddingCache:
    """
    LRU cache for embedding results keyed by content hash.

    Reduces repeated embedding calls for identical texts (e.g., re-mining unchanged
    files, or repeated queries on same content). Zero-cost cache hits.

    TTL 300s (5 min) — embeddings are static for fixed text content.
    Maxsize 512 — fits ~512 × 256-dim × 4bytes ≈ 0.5MB for embeddings alone.
    """

    _MAXSIZE = 512
    _TTL = 300.0  # seconds

    def __init__(self, maxsize: int = 512, ttl_seconds: float = 300.0):
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[list[float], float]] = {}
        self._lock = threading.Lock()

    def _hash_text(self, text: str) -> str:
        """Content hash as cache key — stable across process restarts."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str) -> list[float] | None:
        """Return cached embedding or None."""
        key = self._hash_text(text)
        with self._lock:
            if key not in self._cache:
                return None
            emb, ts = self._cache[key]
            if time.monotonic() - ts > self._ttl:
                del self._cache[key]
                return None
            # Move to end (LRU): pop and re-insert
            val = self._cache.pop(key)
            self._cache[key] = val
            return emb

    def set(self, text: str, embedding: list[float]) -> None:
        """Store embedding with TTL."""
        key = self._hash_text(text)
        with self._lock:
            # Pop and re-insert for LRU ordering
            if key in self._cache:
                del self._cache[key]
            self._cache[key] = (embedding, time.monotonic())
            # Bulk evict if over capacity — single while loop, no per-eviction overhead
            if len(self._cache) > self._maxsize:
                excess = len(self._cache) - self._maxsize
                for _ in range(excess):
                    oldest = next(iter(self._cache))
                    del self._cache[oldest]

    def clear(self) -> None:
        """Clear all entries."""
        with self._lock:
            self._cache.clear()


# Globální singletons
_query_cache = QueryCache(
    maxsize=int(os.environ.get("MEMPALACE_CACHE_SIZE", "256")),
    ttl_seconds=float(os.environ.get("MEMPALACE_CACHE_TTL", "60.0")),
)
_embedding_cache = EmbeddingCache(
    maxsize=int(os.environ.get("MEMPALACE_EMBED_CACHE_SIZE", "512")),
    ttl_seconds=float(os.environ.get("MEMPALACE_EMBED_CACHE_TTL", "300.0")),
)


def get_query_cache() -> QueryCache:
    return _query_cache


def get_embedding_cache() -> EmbeddingCache:
    return _embedding_cache