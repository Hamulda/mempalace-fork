#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Semantic search against the palace.
Returns verbatim text — the actual words, never summaries.
"""

import functools
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .backends import get_backend
from .namespaces import get_collection_name_for_wing
from .query_sanitizer import sanitize_query

logger = logging.getLogger("mempalace_mcp")

_search_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mp_search")

_reranker = None
_reranker_lock = threading.Lock()

def _get_query_cache():
    """Return the canonical query cache singleton.

    Uses the process-global singleton from query_cache.py so that
    search_memories, fastmcp_server, and lance.py all share the same cache.
    """
    from .query_cache import get_query_cache
    return get_query_cache()


def _get_reranker():
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                try:
                    from sentence_transformers import CrossEncoder

                    _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
                    logger.info("Cross-encoder reranker loaded")
                except ImportError:
                    _reranker = False
                    logger.info("sentence-transformers not installed, reranking disabled")
    return _reranker if _reranker is not False else None


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
        collection_name = get_collection_name_for_wing(wing) if wing else cfg.collection_name
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
        source = Path(meta.get("source_file", "?")).name
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
        collection_name = get_collection_name_for_wing(wing) if wing else cfg.collection_name
        cache_key = f"{palace_path}|{collection_name}|{query}|{wing}|{room}|{is_latest}|{agent_id}|{n_results}|{rerank}|{priority_gte}|{priority_lte}"
        cached_result = cache.get_value(cache_key)
        if cached_result is not None:
            return cached_result
    except Exception:
        pass

    try:
        backend = get_backend(cfg.backend)
        collection_name = get_collection_name_for_wing(wing) if wing else cfg.collection_name
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
        hits.append(
            {
                "id": ids[i],
                "text": doc,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(meta.get("source_file", "?")).name,
                "similarity": round(1 - dist, 3),
            }
        )

    # Rerank: re-order using cross-encoder if query has >3 words
    if rerank and len(hits) > 1 and len(query.split()) > 3:
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
            cache.set_value(cache_key, result_dict)
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
    """Clear all cached search results — call after any write."""
    try:
        cache = _get_query_cache()
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
        collection_name = get_collection_name_for_wing(wing) if wing else cfg.collection_name
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
    import asyncio, functools
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _search_executor,
        functools.partial(hybrid_search, query=query, palace_path=palace_path,
            wing=wing, room=room, n_results=n_results, use_kg=use_kg,
            rerank=rerank, agent_id=agent_id, is_latest=is_latest)
    )


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


def is_code_query(query: str) -> bool:
    """Detect if a query looks like a code search (vs prose/memory search)."""
    return bool(_CODE_QUERY_RE.search(query))


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

        # Fetch metadata for each hit from the collection
        doc_ids = [r["document_id"] for r in results]
        hits = []
        for r in results:
            try:
                record = col.get(ids=[r["document_id"]], include=["documents", "metadatas"])
                if record and record.get("ids"):
                    doc = record["documents"][0] if record["documents"] else ""
                    meta = record["metadatas"][0] if record["metadatas"] else {}
                    hits.append({
                        "id": r["document_id"],
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
                continue
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
        collection_name = get_collection_name_for_wing("repo")
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
        # Apply file_path filter
        if file_path:
            vector_hits = [h for h in vector_hits if file_path.lower() in h.get("source_file", "").lower()]
    except Exception as e:
        logger.warning("Vector search in code_search failed: %s", e)

    # Merge vector + FTS5 with RRF
    merged = _rrf_merge([vector_hits, fts5_hits])[:n_results]

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

# Auto-routing: detect query type and route to appropriate specialized search
def auto_search(
    query: str,
    palace_path: str,
    n_results: int = 10,
) -> dict:
    """
    Automatically detect query type and route to appropriate search path.

    Detection rules:
    - code-like query pattern → code_search()
    - Otherwise → hybrid_search() (general search)

    This is the recommended entry point for Claude Code — it handles routing automatically.
    """
    if is_code_query(query):
        return code_search(query, palace_path, n_results=n_results)
    else:
        return hybrid_search(query, palace_path, n_results=n_results)
