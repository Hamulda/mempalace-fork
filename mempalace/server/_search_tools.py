"""
Search/read tools: status, list_wings, list_rooms, taxonomy, search, hybrid, traverse, graph.

AAAK dialect constants live here so they can be imported without the full server.
"""
from datetime import datetime

PALACE_PROTOCOL = """IMPORTANT — MemPalace Memory Protocol:
1. ON WAKE-UP: Call mempalace_status to load palace overview + AAAK spec.
2. BEFORE RESPONDING about any person, project, or past event: call mempalace_kg_query or mempalace_search FIRST. Never guess — verify.
3. IF UNSURE about a fact (name, gender, age, relationship): say "let me check" and query the palace. Wrong is worse than slow.
4. AFTER EACH SESSION: call mempalace_diary_write to record what happened, what you learned, what matters.
5. WHEN FACTS CHANGE: call mempalace_kg_invalidate on the old fact, mempalace_kg_add for the new one.

This protocol ensures the AI KNOWS before it speaks. Storage is not memory — but storage + this protocol = memory."""

AAAK_SPEC = """AAAK is a compressed memory dialect that MemPalace uses for efficient storage.
It is designed to be readable by both humans and LLMs without decoding.

FORMAT:
  ENTITIES: 3-letter uppercase codes. ALC=Alice, JOR=Jordan, RIL=Riley, MAX=Max, BEN=Ben.
  EMOTIONS: *action markers* before/during text. *warm*=joy, *fierce*=determined, *raw*=vulnerable, *bloom*=tenderness.
  STRUCTURE: Pipe-separated fields. FAM: family | PROJ: projects | ⚠: warnings/reminders.
  DATES: ISO format (2026-03-31). COUNTS: Nx = N mentions (e.g., 570x).
  IMPORTANCE: ★ to ★★★★★ (1-5 scale).
  HALLS: hall_facts, hall_events, hall_discoveries, hall_preferences, hall_advice.
  WINGS: wing_user, wing_agent, wing_team, wing_code, wing_<project_name>, wing_hardware, wing_research.
  ROOMS: Hyphenated slugs representing named ideas (e.g., chromadb-setup, gpu-pricing).

EXAMPLE:
  FAM: ALC→♡JOR | 2D(kids): RIL(18,sports) MAX(11,chess+swimming) | BEN(contributor)

Read AAAK naturally — expand codes mentally, treat *markers* as emotional context.
When WRITING AAAK: use entity codes, mark emotions, keep structure tight."""


def register_search_tools(server, backend, config, settings, memory_guard):
    """
    Register all search/read @mcp.tool() as closures over backend/config/kg.
    Called by factory._register_tools().
    """
    from fastmcp import Context
    from ..searcher import (
        search_memories_async, hybrid_search_async, invalidate_query_cache,
        code_search_async, auto_search, is_code_query,
    )
    from ..palace_graph import traverse, find_tunnels, graph_stats
    from ._infrastructure import STATUS_CACHE_TTL

    def _get_collection(create=False):
        try:
            return backend.get_collection(
                settings.db_path, settings.effective_collection_name, create=create
            )
        except Exception:
            return None

    def _no_palace():
        return {"error": "No palace found", "hint": "Run: mempalace init <dir> && mempalace mine <dir>"}

    # ── Status ────────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_status(ctx: Context) -> dict:
        import time
        now = time.time()
        cached_data, cached_ts = server._status_cache.get(settings.db_path)
        if cached_data is not None and (now - cached_ts) < STATUS_CACHE_TTL:
            return cached_data

        col = _get_collection()
        if not col:
            return _no_palace()

        count = col.count()
        wings = {}
        rooms = {}
        try:
            _BATCH = 500
            offset = 0
            while True:
                batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
                metas = batch.get("metadatas", [])
                if not metas:
                    break
                for m in metas:
                    w = m.get("wing", "unknown")
                    r = m.get("room", "unknown")
                    wings[w] = wings.get(w, 0) + 1
                    rooms[r] = rooms.get(r, 0) + 1
                if len(metas) < _BATCH:
                    break
                offset += len(metas)
        except Exception:
            pass

        ctx.debug(f"status returning {count} drawers")
        guard_stats = {}
        if memory_guard is not None:
            try:
                guard_stats = {
                    "pressure": memory_guard.pressure.value,
                    "used_ratio": round(memory_guard.used_ratio, 3),
                    "should_pause_writes": memory_guard.should_pause_writes(),
                }
            except Exception:
                pass
        result = {
            "total_drawers": count,
            "wings": wings,
            "rooms": rooms,
            "palace_path": settings.db_path,
            "protocol": PALACE_PROTOCOL,
            "aaak_dialect": AAAK_SPEC,
            "memory_guard": guard_stats if guard_stats else "inactive",
        }
        server._status_cache.set(settings.db_path, result, now)
        return result

    # ── List tools ───────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_wings(ctx: Context) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        wings = {}
        try:
            _BATCH = 500
            offset = 0
            while True:
                batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
                metas = batch.get("metadatas", [])
                if not metas:
                    break
                for m in metas:
                    w = m.get("wing", "unknown")
                    wings[w] = wings.get(w, 0) + 1
                if len(metas) < _BATCH:
                    break
                offset += len(metas)
        except Exception:
            pass
        return {"wings": wings}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_rooms(ctx: Context, wing: str | None = None) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        rooms = {}
        try:
            _BATCH = 500
            offset = 0
            while True:
                kwargs = {"include": ["metadatas"], "limit": _BATCH, "offset": offset}
                if wing:
                    kwargs["where"] = {"wing": wing}
                batch = col.get(**kwargs)
                metas = batch.get("metadatas", [])
                if not metas:
                    break
                for m in metas:
                    r = m.get("room", "unknown")
                    rooms[r] = rooms.get(r, 0) + 1
                if len(metas) < _BATCH:
                    break
                offset += len(metas)
        except Exception:
            pass
        return {"wing": wing or "all", "rooms": rooms}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_get_taxonomy(ctx: Context) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        taxonomy = {}
        try:
            _BATCH = 500
            offset = 0
            while True:
                batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
                metas = batch.get("metadatas", [])
                if not metas:
                    break
                for m in metas:
                    w = m.get("wing", "unknown")
                    r = m.get("room", "unknown")
                    if w not in taxonomy:
                        taxonomy[w] = {}
                    taxonomy[w][r] = taxonomy[w].get(r, 0) + 1
                if len(metas) < _BATCH:
                    break
                offset += len(metas)
        except Exception:
            pass
        return {"taxonomy": taxonomy}

    @server.tool()
    def mempalace_get_aaak_spec(ctx: Context) -> dict:
        return {"aaak_spec": AAAK_SPEC}

    # ── Search ───────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_embed)
    async def mempalace_search(
        ctx: Context,
        query: str,
        limit: int = 5,
        wing: str | None = None,
        room: str | None = None,
        is_latest: bool | None = None,
        agent_id: str | None = None,
        rerank: bool = False,
    ) -> dict:
        return await search_memories_async(
            query,
            palace_path=settings.db_path,
            wing=wing,
            room=room,
            is_latest=is_latest,
            agent_id=agent_id,
            n_results=limit,
            rerank=rerank,
        )

    @server.tool(timeout=settings.timeout_embed)
    async def mempalace_hybrid_search(
        ctx: Context,
        query: str,
        limit: int = 10,
        wing: str | None = None,
        room: str | None = None,
        use_kg: bool = True,
        rerank: bool = False,
        agent_id: str | None = None,
        is_latest: bool | None = None,
    ) -> dict:
        return await hybrid_search_async(
            query=query, palace_path=settings.db_path, wing=wing, room=room,
            n_results=limit, use_kg=use_kg, rerank=rerank,
            agent_id=agent_id, is_latest=is_latest,
        )

    @server.tool(timeout=settings.timeout_embed)
    def mempalace_check_duplicate(ctx: Context, content: str, threshold: float = 0.9) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        try:
            results = col.query(
                query_texts=[content],
                n_results=5,
                include=["metadatas", "documents", "distances"],
            )
            duplicates = []
            if results["ids"] and results["ids"][0]:
                for i, drawer_id in enumerate(results["ids"][0]):
                    dist = results["distances"][0][i]
                    similarity = round(1 - dist, 3)
                    if similarity >= threshold:
                        meta = results["metadatas"][0][i]
                        doc = results["documents"][0][i]
                        duplicates.append({
                            "id": drawer_id,
                            "wing": meta.get("wing", "?"),
                            "room": meta.get("room", "?"),
                            "similarity": similarity,
                            "content": doc[:200] + "..." if len(doc) > 200 else doc,
                        })
            return {"is_duplicate": len(duplicates) > 0, "matches": duplicates}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_traverse_graph(ctx: Context, start_room: str, max_hops: int = 2) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        return traverse(start_room, col=col, max_hops=max_hops)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_find_tunnels(ctx: Context, wing_a: str | None = None, wing_b: str | None = None) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        return find_tunnels(wing_a, wing_b, col=col)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_graph_stats(ctx: Context) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        return graph_stats(col=col)

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_eval(
        ctx: Context,
        queries: list[str],
        expected_wing: str | None = None,
        n_results: int = 5,
    ) -> dict:
        results_summary = []
        total_similarity = 0.0
        wing_hit_count = 0
        total_results = 0
        for query in queries[:10]:
            result = await search_memories_async(
                query=query, palace_path=settings.db_path, n_results=n_results
            )
            hits = result.get("results", [])
            avg_sim = sum(h.get("similarity", 0) for h in hits) / max(len(hits), 1)
            wing_hits = 0
            if expected_wing and hits:
                wing_hits = sum(1 for h in hits if h.get("wing") == expected_wing)
                wing_hit_count += wing_hits
            total_similarity += avg_sim
            total_results += len(hits)
            results_summary.append({
                "query": query,
                "hit_count": len(hits),
                "avg_similarity": round(avg_sim, 3),
                "wing_precision": round(wing_hits / max(len(hits), 1), 2) if expected_wing else None,
                "top_result": hits[0]["text"][:100] if hits else None,
            })
        n_queries = len(queries[:10])
        return {
            "eval_summary": {
                "queries_tested": n_queries,
                "avg_similarity_across_queries": round(total_similarity / max(n_queries, 1), 3),
                "avg_results_per_query": round(total_results / max(n_queries, 1), 1),
                "wing_precision": round(wing_hit_count / max(total_results, 1), 2) if expected_wing else None,
            },
            "per_query": results_summary,
            "diagnosis": (
                "Good retrieval" if total_similarity / max(n_queries, 1) > 0.7
                else "Low similarity — consider reranking (rerank=True) or refining queries"
            ),
        }
