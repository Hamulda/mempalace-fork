#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Semantic search against the palace.
Returns verbatim text — the actual words, never summaries.
"""

import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .backends import get_backend
from .query_sanitizer import sanitize_query

logger = logging.getLogger("mempalace_mcp")

_search_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mp_search")

_reranker = None
_reranker_lock = threading.Lock()

# Query cache singleton (lazy, thread-safe)
_query_cache = None
_query_cache_lock = threading.Lock()


def _get_query_cache():
    global _query_cache
    if _query_cache is None:
        with _query_cache_lock:
            if _query_cache is None:
                from .query_cache import QueryCache
                _query_cache = QueryCache()
    return _query_cache

# Query cache singleton (lazy, thread-safe)
_query_cache = None
_query_cache_lock = threading.Lock()


def _get_query_cache():
    global _query_cache
    if _query_cache is None:
        with _query_cache_lock:
            if _query_cache is None:
                from .query_cache import QueryCache
                _query_cache = QueryCache()
    return _query_cache


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
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
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

    # Cache lookup before backend call
    try:
        import time
        cache = _get_query_cache()
        cache_key = f"{query}|{wing}|{room}|{is_latest}|{agent_id}|{n_results}|{rerank}"
        if cache_key in cache._cache:
            cached_result, cached_ts = cache._cache[cache_key]
            if time.monotonic() - cached_ts < cache._ttl:
                return cached_result
            else:
                del cache._cache[cache_key]
    except Exception:
        pass

    try:
        cfg = MempalaceConfig()
        backend = get_backend(cfg.backend)
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
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

    # Adaptive top_k — prevent ChromaDB exception when n_results > doc count
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

    hits = []
    for doc, meta, dist in zip(docs, metas, dists):
        hits.append(
            {
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
            import time
            cache = _get_query_cache()
            cache_key = f"{query}|{wing}|{room}|{is_latest}|{agent_id}|{n_results}|{rerank}"
            cache._cache[cache_key] = (result_dict, time.monotonic())
            # Evict if over maxsize
            while len(cache._cache) > cache._maxsize:
                cache._cache.popitem(last=False)
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



# BM25 module-level state
_bm25_index = None
_bm25_corpus = None
_bm25_ids = None
_bm25_lock = threading.Lock()


def _get_bm25(col):
    """Build BM25 index from collection. Returns (bm25, corpus_texts, doc_ids) or (None, None, None)."""
    global _bm25_index, _bm25_corpus, _bm25_ids
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return None, None, None

    with _bm25_lock:
        try:
            data = col.get(include=["documents", "metadatas"], limit=50000)
            docs = data["documents"]
            ids = data["ids"]
            if not docs:
                return None, None, None
            tokenized = [d.lower().split() for d in docs]
            bm25 = BM25Okapi(tokenized)
            return bm25, docs, ids
        except Exception as e:
            logger.warning("BM25 index build failed: %s", e)
            return None, None, None


def _bm25_search(query: str, col, n_results: int = 10, wing: str = None, room: str = None) -> list:
    """Keyword search using BM25. Returns list of hit dicts compatible with search_memories() output."""
    bm25, docs, ids = _get_bm25(col)
    if bm25 is None:
        return []

    try:
        tokenized_query = query.lower().split()
        scores = bm25.get_scores(tokenized_query)
        top_n = min(n_results * 2, len(scores))
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_n]
        max_score = max(scores) if max(scores) > 0 else 1.0

        data = col.get(include=["metadatas"], ids=[ids[i] for i in top_indices])

        hits = []
        for i, idx in enumerate(top_indices):
            if scores[idx] <= 0:
                continue
            meta = data["metadatas"][i] if data["metadatas"] else {}
            if wing and meta.get("wing") != wing:
                continue
            if room and meta.get("room") != room:
                continue
            hits.append({
                "text": docs[idx],
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": meta.get("source_file", "?"),
                "similarity": round(float(scores[idx]) / max_score, 3),
                "source": "bm25",
                "bm25_score": round(float(scores[idx]), 4),
            })
        return hits[:n_results]
    except Exception as e:
        logger.warning("BM25 search failed: %s", e)
        return []


def _rrf_merge(result_lists: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion — combines results from multiple retrieval systems."""
    scores = {}
    for result_list in result_lists:
        for rank, hit in enumerate(result_list):
            key = hit["text"][:80]
            scores[key] = scores.get(key, 0) + 1 / (k + rank + 1)

    seen = {}
    for hit_list in result_lists:
        for hit in hit_list:
            key = hit["text"][:80]
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
) -> dict:
    from datetime import date
    from .config import MempalaceConfig

    # Vrstva 1: ChromaDB semantic search
    chroma = search_memories(
        query=query, palace_path=palace_path, wing=wing, room=room,
        n_results=n_results, is_latest=True, agent_id=agent_id, rerank=rerank
    )
    hits = chroma.get("results", [])

    # Vrstva 1b: BM25 keyword search (zachycuji presne vyrazy, verze, identifikatory)
    bm25_hits = []
    try:
        cfg = MempalaceConfig()
        backend = get_backend(cfg.backend)
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        bm25_hits = _bm25_search(query, col, n_results=n_results, wing=wing, room=room)
    except Exception as e:
        logger.warning("BM25 layer failed: %s", e)

    # Vrstva 2: KG entity search
    kg_hits = []
    if use_kg:
        try:
            from .knowledge_graph import KnowledgeGraph
            from pathlib import Path
            kg_path = str(Path(palace_path) / "knowledge_graph.sqlite3")
            kg = KnowledgeGraph(db_path=kg_path)
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
            kg.close()
        except Exception as e:
            logger.warning("KG layer failed in hybrid_search, ChromaDB only: %s", e)

    # Reciprocal Rank Fusion — kombinuje vysledky ze vsech tri vrstev
    merged = _rrf_merge([hits, bm25_hits, kg_hits])[:n_results]

    return {
        "query": query,
        "filters": {"wing": wing, "room": room, "agent_id": agent_id},
        "results": merged,
        "sources": {"chroma": len(hits), "bm25": len(bm25_hits), "kg": len(kg_hits)},
    }


async def hybrid_search_async(
    query: str, palace_path: str, wing: str = None, room: str = None,
    n_results: int = 10, use_kg: bool = True, rerank: bool = False, agent_id: str = None,
) -> dict:
    import asyncio, functools
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _search_executor,
        functools.partial(hybrid_search, query=query, palace_path=palace_path,
            wing=wing, room=room, n_results=n_results, use_kg=use_kg,
            rerank=rerank, agent_id=agent_id)
    )
