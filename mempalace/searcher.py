#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Semantic search against the palace.
Returns verbatim text — the actual words, never summaries.
"""

import functools
import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .backends import get_backend
from .query_sanitizer import sanitize_query

logger = logging.getLogger("mempalace_mcp")

_search_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mp_search")

_reranker = None
_reranker_lock = threading.Lock()


def _path_contains(haystack: str, needle: str) -> bool:
    """Check if needle is a path component of haystack (path-aware, case-insensitive).

    Matches when:
    - haystack ends with needle (needle is the filename or final dir)
    - haystack contains /needle/ as a path segment (needle is a dir)
    - haystack contains /needle/ at any position (needle is a path component)

    Does NOT match partial path segments (e.g., 'til' does not match '/src/utils.py').
    """
    if not haystack or not needle:
        return False
    hl = haystack.lower()
    nl = needle.lower()
    # Normalize needle: strip leading/trailing slashes for comparison
    nl_norm = nl.strip("/")
    # Exact suffix match (needle is filename or final path component)
    if hl.endswith(nl) or hl.endswith(nl_norm):
        return True
    # /needle/ as path component (needle is a directory name)
    return ("/" + nl_norm + "/") in hl


def _compute_repo_rel_path(source_file: str, common_prefix: str) -> str:
    """Compute repo-relative path from source_file given a common prefix.

    Returns the portion of source_file that is relative to common_prefix.
    If source_file doesn't start with common_prefix, returns source_file unchanged.
    """
    if not source_file or not common_prefix:
        return source_file
    # Ensure common_prefix ends with /
    prefix = common_prefix if common_prefix.endswith("/") else common_prefix + "/"
    if source_file.startswith(prefix):
        return source_file[len(prefix):]
    return source_file


def _add_repo_rel_path(hits: list[dict], source_files: list[str]) -> list[dict]:
    """Add repo_rel_path to hits if a common project prefix can be determined.

    Computes the longest common directory prefix among source_files.
    If that prefix is not "~" or "/" (i.e., it's a real project root), adds repo_rel_path.
    """
    if not hits or not source_files:
        return hits

    # Find common prefix
    if len(source_files) == 1:
        paths_to_check = [source_files[0]]
    else:
        paths_to_check = source_files[:20]  # limit for perf

    # Find longest common directory prefix (handle mixed absolute/relative)
    try:
        common = os.path.commonpath(paths_to_check) if paths_to_check else ""
    except ValueError:
        # Mixed absolute/relative paths - can't compute common path
        return hits

    # commonpath returns "" for empty or single path starting with /
    # Only use if it's a meaningful project root (not just "/" or "~")
    if common and common not in ("/", "~") and not common.startswith("~"):
        for hit in hits:
            sf = hit.get("source_file", "")
            if sf:
                hit["repo_rel_path"] = _compute_repo_rel_path(sf, common)
    return hits

def _get_reranker():
    """Load BGE Reranker v2-m3 with MPS (Metal) acceleration on M1.

    MPS = Metal GPU on Apple Silicon, detected automatically.
    BEIR ~61 vs MiniLM ~57, multilingual (CZ ✅), ~600MB RAM.
    Memory guard: refuse load below 1500MB free to avoid swap on 8GB M1.
    """
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                # Memory guard: refuse load below 800MB free.
                # Reranker loads lazily (not at startup) so this is a bonus, not a lockout.
                # 1500MB was too conservative — would block reranking almost always on 8GB M1.
                try:
                    import psutil
                    free_mb = psutil.virtual_memory().available // 1024 // 1024
                    if free_mb < 800:
                        logger.warning(
                            "Low memory (%dMB free), reranking disabled", free_mb
                        )
                        _reranker = False
                        return None
                except ImportError:
                    pass

                try:
                    from sentence_transformers import CrossEncoder
                    import torch

                    # MPS = Metal GPU on M1/M2/M3, detected automatically
                    device = "mps" if torch.backends.mps.is_available() else "cpu"
                    _reranker = CrossEncoder("BAAI/bge-reranker-v2-m3", device=device)
                    logger.info("BGE Reranker v2-m3 loaded on %s", device.upper())
                except ImportError:
                    _reranker = False
                    logger.info("sentence-transformers not installed, reranking disabled")
    return _reranker if _reranker is not False else None


def warmup_reranker():
    """Eagerly load the Jina Reranker v3 MLX.

    Call this only if reranker_warmup=True in settings (opt-in only).
    On M1 Air 8GB this costs ~600MB RAM + ~5-10s on first load.
    Safe to call multiple times — no-op if already loaded.
    """
    _get_reranker()


class SearchError(Exception):
    """Raised when search cannot proceed (e.g. no palace found)."""


def _build_where_filter(
    wing: str = None,
    room: str = None,
    is_latest: bool = None,
    agent_id: str = None,
    priority_gte: int = None,
    priority_lte: int = None,
) -> dict:
    conditions = []
    if wing:
        conditions.append({"wing": {"$eq": wing}})
    if room:
        conditions.append({"room": {"$eq": room}})
    if is_latest is not None:
        conditions.append({"is_latest": {"$eq": is_latest}})
    if agent_id:
        conditions.append({"agent_id": {"$eq": agent_id}})
    if priority_gte is not None:
        conditions.append({"priority": {"$gte": priority_gte}})
    if priority_lte is not None:
        conditions.append({"priority": {"$lte": priority_lte}})

    if len(conditions) == 0:
        return {}
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def search(query: str, palace_path: str, wing: str = None, room: str = None, n_results: int = 5):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project) or room (aspect).
    """
    from .config import MempalaceConfig

    query = sanitize_query(query)
    if not query:
        print("\n  Query empty after sanitization (possible injection detected).")
        return

    try:
        cfg = MempalaceConfig()
        backend = get_backend(cfg.backend)
        collection_name = cfg.collection_name
        col = backend.get_collection(palace_path, collection_name, create=False)
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print(f"  Run: mempalace init <dir> then mempalace mine <dir>")
        raise SearchError(f"No palace found at {palace_path}")

    where = _build_where_filter(wing=wing, room=room)

    try:
        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = col.query(**kwargs)

    except Exception as e:
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]

    if not docs:
        print(f'\n  No results found for: "{query}"')
        return

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    print(f"{'=' * 60}\n")

    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
        similarity = round(1 - dist, 3)
        source = meta.get("source_file", "?")
        wing_name = meta.get("wing", "?")
        room_name = meta.get("room", "?")

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        print(f"      Match:  {similarity}")
        print()
        # Print the verbatim text, indented
        for line in doc.strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'─' * 56}")

    print()


def search_memories(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    is_latest: bool = None,
    agent_id: str = None,
    priority_gte: int = None,
    priority_lte: int = None,
    n_results: int = 5,
    rerank: bool = False,
) -> dict:
    """
    Programmatic search — returns a dict instead of printing.
    Used by the MCP server and other callers that need data.
    """
    from .config import MempalaceConfig

    def _get_query_cache():
        from .query_cache import get_query_cache
        return get_query_cache()

    query = sanitize_query(query)
    if not query:
        return {"query": "", "filters": {}, "results": [], "error": "Query was empty after sanitization"}

    try:
        cfg = MempalaceConfig()
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        return {"error": "Config unavailable"}

    # Cache lookup before backend call — key includes palace_path + collection_name
    try:
        cache = _get_query_cache()
        collection_name = cfg.collection_name
        cache_key = f"{palace_path}|{collection_name}|{query}|{wing}|{room}|{is_latest}|{agent_id}|{n_results}|{rerank}|{priority_gte}|{priority_lte}"
        cached_result = cache.get_value(cache_key, palace_path=palace_path, collection=collection_name)
        if cached_result is not None:
            return cached_result
    except Exception:
        pass

    try:
        backend = get_backend(cfg.backend)
        collection_name = cfg.collection_name
        col = backend.get_collection(palace_path, collection_name, create=False)
    except Exception as e:
        logger.error("No palace found at %s: %s", palace_path, e)
        return {
            "error": "No palace found",
            "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
        }

    where = _build_where_filter(
        wing=wing,
        room=room,
        is_latest=is_latest,
        agent_id=agent_id,
        priority_gte=priority_gte,
        priority_lte=priority_lte,
    )

    # Adaptive top_k — prevent exception when n_results > doc count
    try:
        actual_count = col.count()
        if actual_count < n_results:
            n_results = max(1, actual_count)
    except Exception:
        pass

    try:
        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = col.query(**kwargs)
    except Exception as e:
        return {"error": f"Search error: {e}"}

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]
    ids = results["ids"][0]

    hits = []
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
        raw_source = meta.get("source_file", "?")
        hits.append(
            {
                "id": ids[i],
                "text": doc,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": raw_source,
                "similarity": round(1 - dist, 3),
            }
        )

    # Rerank: re-order using BGE Reranker v2-m3 for complex semantic queries.
    # MPS-accelerated on M1, multilingual (CZ ✅), BEIR ~61 vs ~57 for MiniLM.
    # Memory-guarded: disabled below 1500MB free; shortlist capped at _RERANK_SHORTLIST_MAX.
    if rerank and _should_rerank(query, len(hits)):
        reranker = _get_reranker()
        if reranker is not None:
            try:
                pairs = [[query, h["text"]] for h in hits]
                scores = reranker.predict(pairs)
                hits_scored = sorted(zip(scores, hits), key=lambda x: x[0], reverse=True)
                hits = []
                for score, h in hits_scored:
                    h["rerank_score"] = round(float(score), 4)
                    hits.append(h)
            except Exception as e:
                logger.warning("Reranking failed, cosine order preserved: %s", e)

    # Add repo_rel_path if a common project prefix can be determined
    source_files = [h.get("source_file", "") for h in hits]
    hits = _add_repo_rel_path(hits, source_files)

    result_dict = {
        "query": query,
        "filters": {
            "wing": wing, "room": room,
            "is_latest": is_latest, "agent_id": agent_id,
            "priority_gte": priority_gte, "priority_lte": priority_lte,
        },
        "results": hits,
    }

    # Cache result (skip errors)
    if "error" not in result_dict:
        try:
            cache = _get_query_cache()
            cache_key = f"{palace_path}|{cfg.collection_name}|{query}|{wing}|{room}|{is_latest}|{agent_id}|{n_results}|{rerank}|{priority_gte}|{priority_lte}"
            cache.set_value(cache_key, result_dict, palace_path=palace_path, collection=cfg.collection_name)
        except Exception:
            pass

    return result_dict


async def search_memories_async(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    is_latest: bool = None,
    agent_id: str = None,
    priority_gte: int = None,
    priority_lte: int = None,
    n_results: int = 5,
    rerank: bool = False,
) -> dict:
    """Non-blocking wrapper — používej v async kontextu (fastmcp_server.py)."""
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _search_executor,
        functools.partial(
            search_memories,
            query=query,
            palace_path=palace_path,
            wing=wing,
            room=room,
            is_latest=is_latest,
            agent_id=agent_id,
            priority_gte=priority_gte,
            priority_lte=priority_lte,
            n_results=n_results,
            rerank=rerank,
        ),
    )



# KG singleton — reused across hybrid_search calls, thread-safe
_kg_instance = None
_kg_path_cached: str | None = None
_kg_lock = threading.Lock()


def _get_kg(palace_path: str):
    """Return cached KnowledgeGraph instance. Thread-safe, reuses connection."""
    global _kg_instance, _kg_path_cached
    from pathlib import Path
    kg_path = str(Path(palace_path) / "knowledge_graph.sqlite3")

    if _kg_instance is not None and _kg_path_cached == kg_path:
        return _kg_instance

    with _kg_lock:
        if _kg_instance is not None and _kg_path_cached == kg_path:
            return _kg_instance
        try:
            from .knowledge_graph import KnowledgeGraph
            if _kg_instance is not None:
                try:
                    _kg_instance.close()
                except Exception:
                    pass
            _kg_instance = KnowledgeGraph(db_path=kg_path)
            _kg_path_cached = kg_path
            logger.debug("KG singleton created for %s", kg_path)
        except Exception as e:
            logger.warning("KG singleton init failed: %s", e)
            return None
    return _kg_instance


def invalidate_query_cache() -> None:
    """
    Clear ALL query cache entries (full cross-palace flush).

    Called after write operations: add_drawer, delete_drawer, diary_write,
    remember_code, consolidate. These are infrequent enough that a full
    clear is acceptable and avoids cross-palace stale data.

    NOTE: This is a brute-force clear (removes every entry from the cache).
    invalidate_collection(palace_path, collection) does targeted removal for
    only those palace+collection entries (both get/set and get_value/set_value
    interfaces) — use it when you need per-palace granularity and know the
    palace_path+collection of the write.
    """
    try:
        from .query_cache import get_query_cache
        cache = get_query_cache()
        cache.clear()
    except Exception:
        pass
    logger.debug("Query cache cleared")



def _rrf_merge(result_lists: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion — combines results from multiple retrieval systems."""
    scores = {}
    for result_list in result_lists:
        for rank, hit in enumerate(result_list):
            key = hit.get("id") or hashlib.md5(hit["text"].encode(), usedforsecurity=False).hexdigest()[:16]
            scores[key] = scores.get(key, 0) + 1 / (k + rank + 1)

    seen = {}
    for hit_list in result_lists:
        for hit in hit_list:
            key = hit.get("id") or hashlib.md5(hit["text"].encode(), usedforsecurity=False).hexdigest()[:16]
            if key not in seen:
                seen[key] = hit
                seen[key]["rrf_score"] = round(scores.get(key, 0), 6)

    sorted_keys = sorted(scores, key=lambda k: scores[k], reverse=True)
    return [seen[k] for k in sorted_keys if k in seen]


def hybrid_search(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    n_results: int = 10,
    use_kg: bool = True,
    rerank: bool = False,
    agent_id: str = None,
    is_latest: bool | None = None,
) -> dict:
    from datetime import date
    from .config import MempalaceConfig

    # Layer 1: vector similarity search
    results = search_memories(
        query=query, palace_path=palace_path, wing=wing, room=room,
        n_results=n_results, is_latest=is_latest, agent_id=agent_id, rerank=rerank
    )
    hits = results.get("results", [])

    # Vrstva 1b: FTS5 keyword search (persistent SQLite index, zero memory at rest)
    fts5_hits = []
    try:
        cfg = MempalaceConfig()
        backend = get_backend(cfg.backend)
        collection_name = cfg.collection_name
        col = backend.get_collection(palace_path, collection_name, create=False)
        fts5_hits = _fts5_search(query, col, palace_path, n_results=n_results, wing=wing, room=room)
    except Exception as e:
        logger.warning("FTS5 layer failed: %s", e)

    # Vrstva 2: KG entity search
    kg_hits = []
    if use_kg:
        kg = _get_kg(palace_path)
        if kg is None:
            logger.warning("KG unavailable, skipping KG layer in hybrid_search")
        else:
            try:
                today = date.today().isoformat()
                tokens = [t.lower() for t in query.split() if len(t) > 3]
                seen = set()
                for token in tokens[:5]:
                    for triple in kg.query_entity(token, as_of=today)[:3]:
                        key = f"{triple['subject']}_{triple['predicate']}_{triple['object']}"
                        if key not in seen:
                            seen.add(key)
                            kg_hits.append({
                                "text": f"{triple['subject']} {triple['predicate']} {triple['object']}",
                                "wing": "knowledge_graph",
                                "room": triple["predicate"],
                                "source_file": "knowledge_graph.sqlite3",
                                "similarity": triple.get("confidence", 0.8),
                                "source": "kg",
                            })
            except Exception as e:
                logger.warning("KG layer failed in hybrid_search, vector search only: %s", e)

    # Reciprocal Rank Fusion — kombinuje vysledky ze vsech tri vrstev
    merged = _rrf_merge([hits, fts5_hits, kg_hits])[:n_results]

    return {
        "query": query,
        "filters": {"wing": wing, "room": room, "agent_id": agent_id},
        "results": merged,
        "sources": {"vector": len(hits), "fts5": len(fts5_hits), "kg": len(kg_hits)},
    }


async def hybrid_search_async(
    query: str, palace_path: str, wing: str = None, room: str = None,
    n_results: int = 10, use_kg: bool = True, rerank: bool = False,
    agent_id: str = None, is_latest: bool | None = None,
) -> dict:
    """Parallel 3-layer hybrid search: vector + FTS5 + KG run concurrently.

    Latency = max(layer times) instead of sum. Uses asyncio.to_thread for
    CPU-bound layers (vector search, FTS5) and direct call for KG.
    """
    import asyncio

    from datetime import date
    from .config import MempalaceConfig

    def _vector_layer():
        return search_memories(
            query=query, palace_path=palace_path, wing=wing, room=room,
            n_results=n_results, is_latest=is_latest, agent_id=agent_id, rerank=rerank
        )

    def _fts5_layer():
        try:
            cfg = MempalaceConfig()
            backend = get_backend(cfg.backend)
            collection_name = cfg.collection_name
            col = backend.get_collection(palace_path, collection_name, create=False)
            return _fts5_search(query, col, palace_path, n_results=n_results, wing=wing, room=room)
        except Exception as e:
            logger.warning("FTS5 layer failed: %s", e)
            return []

    def _kg_layer():
        if not use_kg:
            return []
        try:
            kg = _get_kg(palace_path)
            if kg is None:
                return []
            today = date.today().isoformat()
            tokens = [t.lower() for t in query.split() if len(t) > 3]
            seen = set()
            kg_hits = []
            for token in tokens[:5]:
                for triple in kg.query_entity(token, as_of=today)[:3]:
                    key = f"{triple['subject']}_{triple['predicate']}_{triple['object']}"
                    if key not in seen:
                        seen.add(key)
                        kg_hits.append({
                            "text": f"{triple['subject']} {triple['predicate']} {triple['object']}",
                            "wing": "knowledge_graph",
                            "room": triple["predicate"],
                            "source_file": "knowledge_graph.sqlite3",
                            "similarity": triple.get("confidence", 0.8),
                            "source": "kg",
                        })
            return kg_hits
        except Exception as e:
            logger.warning("KG layer failed in hybrid_search: %s", e)
            return []

    # Run all 3 layers concurrently — latency = max, not sum
    vector_results, fts5_hits, kg_hits = await asyncio.gather(
        asyncio.to_thread(_vector_layer),
        asyncio.to_thread(_fts5_layer),
        asyncio.to_thread(_kg_layer),
    )

    hits = vector_results.get("results", [])

    # RRF merge
    merged = _rrf_merge([hits, fts5_hits, kg_hits])[:n_results]

    return {
        "query": query,
        "filters": {"wing": wing, "room": room, "agent_id": agent_id},
        "results": merged,
        "sources": {"vector": len(hits), "fts5": len(fts5_hits), "kg": len(kg_hits)},
    }


# =============================================================================
# SPLIT RETRIEVAL PATHS
# =============================================================================

import re as _re

# Patterns that strongly indicate a code query (vs prose/diary query)
_CODE_QUERY_PATTERNS = [
    r'\bdef\s+\w+',          # Python function definition
    r'\bfunction\s+\w+',     # JS/TS function
    r'\bclass\s+\w+',       # class definition
    r'\bimport\s+\w+',      # import statement
    r'^from\s+\w+\s+import\b',  # from ... import (at start of query)
    r'\brequire\s*\(',      # CommonJS require
    r'\bexport\s+',         # ES module export
    r'\blet\s+\w+',         # JS let
    r'\bconst\s+\w+',       # JS const
    r'=>',                  # arrow function
    r'\.py\b',              # .py file reference
    r'\.js\b',              # .js file reference
    r'\.ts\b',              # .ts file reference
    r'\.go\b',              # .go file reference
    r'\.rs\b',              # .rs file reference
    r'\.java\b',            # .java file reference
    r'\w+\(\)',             # function call with ()
    r'\w+\.\w+\(',          # method call
    r'\b[A-Z][a-zA-Z0-9]+\s*\(',  # CamelCase function call
    r'\bself\.',            # Python self
    r'\bthis\.',            # JS this
    r'::',                  # C++/Rust namespace/path operator
]

_CODE_QUERY_RE = _re.compile("|".join(_CODE_QUERY_PATTERNS), _re.IGNORECASE)


# Query complexity tiers — used to gate expensive operations (rerank, deep retrieval).
# Budget-aware defaults for M1 Air 8GB under concurrent sessions.
_QUERY_COMPLEXITY_RE = _re.compile(
    r"(?:\bdef\s+\w+|\bclass\s+\w+|\bfunction\s+\w+|\bimport\s+\w+|"
    r"^from\s+\w+\s+import\b|\brequire\s*\(|\bexport\s+|\blet\s+\w+|"
    r"\bconst\s+\w+|=>|\.py\b|\.js\b|\.ts\b|\.go\b|\.rs\b|\.java\b|"
    r"\w+\(\)|\w+\.\w+\(|\b[A-Z][a-zA-Z0-9]+\s*\(|\bself\.|\bthis\.|::)",
    _re.IGNORECASE,
)

# Path-like patterns: /component/file or file.ext — FTS5-exact, no vector needed
_PATH_LIKE_RE = _re.compile(
    r"(?:^|/)\.?[\w\-]+/\.?(?:[\w\-]+/?)*\.([\w\-]+)$|"  # /path/to/file.ext or file.ext
    r"^[\w\-]+[/\\][\w\-]",                               # relative/path or relative\path
    _re.IGNORECASE,
)

# Shortlist ceiling for rerank — Jina MLX is ~4x faster than MiniLM CPU,
# safe to rerank more candidates on M1 Air 8GB
_RERANK_SHORTLIST_MAX = 10


def _query_complexity(query: str) -> str:
    """
    Classify query into complexity tiers for retrieval budgeting.

    Returns:
      path    — path literal or file reference (e.g. src/utils/auth.py, *.py)
      code    — code pattern detected (def, class, import, etc.)
      simple  — short, <=3 words, no strong code signal
      complex — semantic, >=4 words, no code signal  [rerank candidate]
    """
    if _PATH_LIKE_RE.search(query):
        return "path"
    if _CODE_QUERY_RE.search(query):
        return "code"
    if len(query.split()) <= 3:
        return "simple"
    return "complex"


def _should_rerank(query: str, n_results: int) -> bool:
    """
    Decide whether to rerank for a given query + top_k.

    Budget-aware: M1 Air 8GB + 6 concurrent sessions means conservative.
    Rerank only when:
      1. Complexity is "complex" (semantic, >=4 words)
      2. We have enough hits to justify cross-encoder pass (> 1)
      3. Requested shortlist fits within budget ceiling
    """
    if _query_complexity(query) != "complex":
        return False
    if n_results <= 1:
        return False
    if n_results > _RERANK_SHORTLIST_MAX:
        return False
    return True


def is_code_query(query: str) -> bool:
    """Detect if a query looks like a code search (vs prose/memory search)."""
    return bool(_CODE_QUERY_RE.search(query))


def is_path_query(query: str) -> bool:
    """Detect if a query is a path literal or file reference."""
    return bool(_PATH_LIKE_RE.search(query))


def query_complexity(query: str) -> str:
    """Public API for query complexity classification."""
    return _query_complexity(query)


def _fts5_search(
    query: str,
    col,
    palace_path: str,
    n_results: int = 10,
    wing: str = None,
    room: str = None,
    language: str = None,
) -> list:
    """Keyword search via FTS5. Returns hits with document_id, score, metadata."""
    try:
        from .lexical_index import KeywordIndex

        idx = KeywordIndex.get(palace_path)
        results = idx.search(
            query, n_results=n_results * 3,  # fetch more, filter below
            wing=wing, room=room, language=language
        )
        if not results:
            return []

        # Batch fetch all document metadata in a single round-trip (was N round-trips)
        doc_ids = [r["document_id"] for r in results]
        hits = []
        try:
            batch = col.get(ids=doc_ids, include=["documents", "metadatas"])
            returned_ids = batch.get("ids", []) if batch else []
            docs_map = {id_: doc for id_, doc in zip(returned_ids, batch.get("documents", []))}
            metas_map = {id_: meta for id_, meta in zip(returned_ids, batch.get("metadatas", []))}

            for r in results:
                doc_id = r["document_id"]
                doc = docs_map.get(doc_id, "")
                meta = metas_map.get(doc_id, {})
                hits.append({
                    "id": doc_id,
                    "text": doc,
                    "wing": meta.get("wing", r.get("wing", "")),
                    "room": meta.get("room", r.get("room", "")),
                    "source_file": meta.get("source_file", "?"),
                    "similarity": max(0.0, 1.0 - abs(r["score"]) / 10),  # Convert FTS5 score
                    "source": "fts5",
                    "fts5_score": round(r["score"], 4),
                    "language": meta.get("language", r.get("language", "")),
                })
        except Exception:
            # Fallback: skip metadata enrichment (return partial hits)
            for r in results:
                hits.append({
                    "id": r["document_id"],
                    "text": "",
                    "wing": r.get("wing", ""),
                    "room": r.get("room", ""),
                    "source_file": "?",
                    "similarity": max(0.0, 1.0 - abs(r["score"]) / 10),
                    "source": "fts5",
                    "fts5_score": round(r["score"], 4),
                    "language": r.get("language", ""),
                })
        return hits[:n_results]
    except Exception as e:
        logger.warning("FTS5 search failed: %s", e)
        return []


def code_search(
    query: str,
    palace_path: str,
    n_results: int = 10,
    language: str = None,
    symbol_name: str = None,
    file_path: str = None,
    include_prose: bool = False,
) -> dict:
    """
    Specialized retrieval for code content.

    Filters: wing=repo, is_latest=True
    Sources: vector search + FTS5 (for exact identifier match) + optional symbol filter

    Args:
        query: search query
        palace_path: path to palace
        n_results: number of results
        language: filter by programming language (e.g. "Python", "JavaScript")
        symbol_name: filter by exact symbol name (function/class name)
        file_path: filter by source file path (substring match)
        include_prose: if False (default), excludes prose/markdown files

    Returns:
        dict with results list, sources dict
    """
    from .config import MempalaceConfig

    query = sanitize_query(query)
    if not query:
        return {"query": "", "filters": {}, "results": [], "error": "Query empty after sanitization"}

    try:
        cfg = MempalaceConfig()
        backend = get_backend(cfg.backend)
        collection_name = cfg.collection_name
        col = backend.get_collection(palace_path, collection_name, create=False)
    except Exception:
        return {"query": query, "filters": {}, "results": [], "error": f"No palace at {palace_path}"}

    # Build filters
    base_where = {"wing": "repo", "is_latest": True}

    # FTS5 keyword search (exact identifier matching)
    fts5_hits = []
    if include_prose or language != "Markdown":
        fts5_hits = _fts5_search(query, col, palace_path, n_results=n_results, language=language)

    # Vector search
    vector_hits = []
    try:
        results = search_memories(
            query=query, palace_path=palace_path,
            wing="repo", room=None,
            is_latest=True, n_results=n_results,
        )
        vector_hits = results.get("results", [])
        # Apply language filter post-query
        if language:
            vector_hits = [h for h in vector_hits if h.get("language") == language]
        # Apply symbol_name filter
        if symbol_name:
            vector_hits = [h for h in vector_hits if symbol_name.lower() in h.get("text", "").lower()]
        # Apply file_path filter — path-aware, case-insensitive
        if file_path:
            vector_hits = [h for h in vector_hits if _path_contains(h.get("source_file", ""), file_path)]
    except Exception as e:
        logger.warning("Vector search in code_search failed: %s", e)

    # Merge vector + FTS5 with RRF
    merged = _rrf_merge([vector_hits, fts5_hits])[:n_results]
    # Add repo_rel_path
    source_files = [h.get("source_file", "") for h in merged]
    merged = _add_repo_rel_path(merged, source_files)

    return {
        "query": query,
        "filters": {"language": language, "symbol_name": symbol_name, "file_path": file_path},
        "results": merged,
        "sources": {"vector": len(vector_hits), "fts5": len(fts5_hits)},
    }


def code_search_async(
    query: str,
    palace_path: str,
    n_results: int = 10,
    language: str = None,
    symbol_name: str = None,
    file_path: str = None,
    include_prose: bool = False,
) -> dict:
    import asyncio, functools
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(
        _search_executor,
        functools.partial(code_search, query=query, palace_path=palace_path,
            n_results=n_results, language=language, symbol_name=symbol_name,
            file_path=file_path, include_prose=include_prose)
    )


async def auto_search(
    query: str,
    palace_path: str,
    n_results: int = 10,
) -> dict:
    """
    Automatically detect query type and route to appropriate specialized search.

    Detection rules:
    - path-like query → FTS5-only path lookup (no vector, no rerank)
    - code-like query pattern → code_search() (vector + FTS5)
    - Otherwise → hybrid_search_async() (semantic + FTS5 + KG, parallel)

    Rerank is only applied to complex semantic queries via _should_rerank().

    This is the recommended entry point for Claude Code — it handles routing automatically.
    """
    complexity = _query_complexity(query)
    if complexity == "path":
        # Path queries get FTS5-only (exact, no vector, no rerank)
        try:
            cfg = MempalaceConfig()
            backend = get_backend(cfg.backend)
            collection_name = cfg.collection_name
            col = backend.get_collection(palace_path, collection_name, create=False)
            fts5_hits = _fts5_search(query, col, palace_path, n_results=n_results)
            if fts5_hits:
                return {
                    "query": query,
                    "filters": {"complexity": complexity},
                    "results": fts5_hits[:n_results],
                    "sources": {"fts5": len(fts5_hits)},
                }
        except Exception:
            pass
        # Fall back to code_search if FTS5 finds nothing — inject complexity into result
        result = await code_search_async(query, palace_path, n_results=n_results)
        result["filters"]["complexity"] = complexity
        return result
    elif complexity == "code":
        # code_search returns its own filters dict — inject complexity
        result = await code_search_async(query, palace_path, n_results=n_results)
        result["filters"]["complexity"] = complexity
        return result
    else:
        # simple or complex — parallel async layers
        result = await hybrid_search_async(query, palace_path, n_results=n_results)
        result["filters"]["complexity"] = complexity
        return result

