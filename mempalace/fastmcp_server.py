#!/usr/bin/env python3
"""
MemPalace FastMCP Server — read/write palace access for Claude Code
===============================================================
Migrated from low-level MCP SDK to FastMCP v3.
Middleware: ResponseCaching + CacheInvalidation + EmbedCircuitBreaker

Install: claude mcp add mempalace -- python -m mempalace.fastmcp_server [--palace /path/to/palace]

Factory pattern (Sprint F155): create_server(settings) returns an isolated FastMCP
instance. Tests get their own server with tmp_path DB. Production uses the singleton.
"""

import argparse
import os
import sys
import json
import logging
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP, Context
from fastmcp.resources import DirectoryResource
from starlette.responses import JSONResponse
from starlette.routing import Route

from .session_registry import SessionRegistry
from .write_coordinator import WriteCoordinator
from .claims_manager import ClaimsManager
from .handoff_manager import HandoffManager
from .decision_tracker import DecisionTracker
from .wakeup_context import build_wakeup_context
from .config import MempalaceConfig, sanitize_name, sanitize_content
from .middleware import build_middleware_stack
from .settings import settings, MemPalaceSettings
from .version import __version__
from .searcher import (
    search_memories, search_memories_async, hybrid_search_async,
    invalidate_query_cache, code_search_async, auto_search,
    is_code_query,
)
from .palace_graph import traverse, find_tunnels, graph_stats
from .knowledge_graph import KnowledgeGraph
from .backends import get_backend
from .entity_detector import extract_candidates

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("mempalace_mcp")

# WAL async executor — offloads file I/O from async tool handlers
_wal_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mp_wal")

# Background work executor — bounded, prevents thread storm on M1/8GB
# max_workers=2: one for general_extractor, one for background tasks
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mp_bg")

# Status cache — wings/rooms aggregation cached for 60s
_status_cache: dict = {"data": None, "ts": 0.0}
_STATUS_CACHE_TTL: float = 60.0


def _wal_log_async(operation: str, params: dict, result: dict = None, wal_file: Path | None = None):
    """Non-blocking WAL write — offloads file I/O from async tool handlers."""
    _wal_executor.submit(_wal_log, operation, params, result, wal_file)


# ═══════════════════════════════════════════════════════════════════
# WRITE-AHEAD LOG

def _get_wal_path(wal_dir: str | None = None) -> Path:
    """Return WAL file path, creating directory if needed."""
    wal_path = Path(wal_dir or os.path.expanduser("~/.mempalace/wal"))
    wal_path.mkdir(parents=True, exist_ok=True)
    try:
        wal_path.chmod(0o700)
    except (OSError, NotImplementedError):
        pass
    return wal_path / "write_log.jsonl"


def _wal_log(operation: str, params: dict, result: dict = None, wal_file: Path | None = None):
    """Append a write operation to the write-ahead log."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation,
        "params": params,
        "result": result,
    }
    wal_path = wal_file or _get_wal_path()
    try:
        with open(wal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        try:
            wal_path.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
    except Exception as e:
        logger.error(f"WAL write failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# AAAK DIALECT SPEC
# ═══════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════
# SERVER FACTORY
# ═══════════════════════════════════════════════════════════════════

def create_server(settings: MemPalaceSettings | None = None, shared_server_mode: bool = False) -> FastMCP:
    """
    Factory funkce — vytvoří izolovanou FastMCP instanci.

    Použití v produkci: mcp = create_server()
    Použití v testech:  mcp = create_server(settings=test_settings)
    """
    if settings is None:
        settings = MemPalaceSettings()

    # Ensure db_path directory exists
    db_path = Path(settings.db_path)
    db_path.mkdir(parents=True, exist_ok=True)

    # Initialize config and knowledge graph
    config = MempalaceConfig(config_dir=str(db_path.parent.parent))
    backend = get_backend(settings.db_backend)

    # Create fresh middleware stack
    middleware_stack = build_middleware_stack(settings)

    # Create server
    server = FastMCP("MemPalace")
    for mw in middleware_stack:
        server.add_middleware(mw)

    # Phase 1: Session Registry + Write Coordinator for multi-session
    # Phase 3: ClaimsManager + HandoffManager + DecisionTracker for session coordination
    if shared_server_mode or settings.transport == "http":
        registry = SessionRegistry(config.palace_path)
        coordinator = WriteCoordinator(config.palace_path)
        claims_mgr = ClaimsManager(config.palace_path)
        handoff_mgr = HandoffManager(config.palace_path)
        decision_tracker = DecisionTracker(config.palace_path)

        # Attach to server for tool access (setattr bypasses FastMCP's MemoryStore)
        setattr(server, "_session_registry", registry)
        setattr(server, "_write_coordinator", coordinator)
        setattr(server, "_claims_manager", claims_mgr)
        setattr(server, "_handoff_manager", handoff_mgr)
        setattr(server, "_decision_tracker", decision_tracker)

    # Health check endpoint — lightweight, no MCP handshake needed
    @server.custom_route("/health", methods=["GET"], name="health")
    async def health_check(request):
        from starlette.requests import Request
        return JSONResponse({"status": "ok", "service": "mempalace"})

    # Skills as MCP resources — Claude Code can read skill://... URIs
    # Wrap in try/except to handle pydantic validation gracefully
    try:
        skills_path = Path(__file__).parent / "skills"
        if skills_path.exists() and any(skills_path.iterdir()):
            server.add_resource(DirectoryResource(
                name="palace_skills",
                title="MemPalace Skills",
                description="Guides for init, mine, search, status, and help commands",
                path=str(skills_path),
                pattern="*.md",
                uri="mempalace://skills/",
            ))
    except Exception:
        # Skip if validation fails (e.g., DirectoryResource requires uri field)
        pass

    # Register all tools with closures over backend/config/kg
    _register_tools(server, backend, config, settings)

    # Warmup reranker in background — avoid 2s latency on first search
    import threading
    def _warmup_reranker():
        try:
            from .searcher import _get_reranker
            _get_reranker()
        except Exception:
            pass
    threading.Thread(target=_warmup_reranker, daemon=True, name="reranker_warmup").start()

    return server


def _register_tools(server, backend, config, settings):
    """Register all 27 @mcp.tool() as closures over backend/config/kg."""

    def _get_collection(create=False):
        """Return the palace collection using the configured backend."""
        try:
            return backend.get_collection(
                settings.db_path, settings.collection_name, create=create
            )
        except Exception:
            return None

    def _no_palace():
        return {
            "error": "No palace found",
            "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
        }

    def _invalidate_status_cache():
        _status_cache["data"] = None
        _status_cache["ts"] = 0.0

    # MemoryGuard singleton — blocks writes when memory pressure is critical
    _memory_guard = None
    try:
        from .memory_guard import MemoryGuard
        _memory_guard = MemoryGuard.get()
    except (ImportError, Exception) as e:
        logger.debug("memory_guard unavailable: %s", e)

    # ── READ TOOLS ────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_status(ctx: Context) -> dict:
        """[MEMPALACE] Palace overview — total drawers, wing and room counts."""
        import time
        now = time.time()
        if _status_cache["data"] and (now - _status_cache["ts"]) < _STATUS_CACHE_TTL:
            return _status_cache["data"]

        col = _get_collection()
        if not col:
            return _no_palace()

        count = col.count()
        wings = {}
        rooms = {}
        try:
            # Iterative aggregation — no fixed limit, processes ALL records
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
        if _memory_guard is not None:
            try:
                guard_stats = {
                    "pressure": _memory_guard.pressure.value,
                    "used_ratio": round(_memory_guard.used_ratio, 3),
                    "should_pause_writes": _memory_guard.should_pause_writes(),
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
        _status_cache["data"] = result
        _status_cache["ts"] = now
        return result

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_wings(ctx: Context) -> dict:
        """[MEMPALACE] List all wings with drawer counts."""
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
        """[MEMPALACE] List rooms within a wing (or all rooms if no wing given)."""
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
        """[MEMPALACE] Full taxonomy: wing → room → drawer count."""
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
        """[MEMPALACE] Get the AAAK dialect specification — the compressed memory format MemPalace uses."""
        return {"aaak_spec": AAAK_SPEC}

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_eval(
        ctx: Context,
        queries: list[str],
        expected_wing: str | None = None,
        n_results: int = 5,
    ) -> dict:
        """[MEMPALACE] Evaluate retrieval quality: run queries and report hit rates, avg similarity.
        Use to diagnose whether palace is finding relevant memories.
        queries: list of test queries (e.g. ["project deadline", "API design decision"])
        expected_wing: if set, measures what % of top results are in the right wing
        """
        from .searcher import search_memories_async

        results_summary = []
        total_similarity = 0.0
        wing_hit_count = 0
        total_results = 0

        for query in queries[:10]:  # max 10 queries per eval call
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
        """[MEMPALACE] Semantic search. Returns verbatim drawer content with similarity scores."""
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
        """[MEMPALACE] Hybrid search: semantic (LanceDB) + keyword (BM25) + KG combined.
        is_latest=True — pouze aktuální záznamy (default chování).
        is_latest=None — vše včetně historických.
        use_kg=True adds entity relationship facts alongside vector matches.
        rerank=True — cross-encoder reranking (pomalejší, přesnější)."""
        return await hybrid_search_async(
            query=query, palace_path=settings.db_path, wing=wing, room=room,
            n_results=limit, use_kg=use_kg, rerank=rerank,
            agent_id=agent_id, is_latest=is_latest,
        )

    @server.tool(timeout=settings.timeout_embed)
    def mempalace_check_duplicate(
        ctx: Context,
        content: str,
        threshold: float = 0.9,
    ) -> dict:
        """[MEMPALACE] Check if content already exists in the palace before filing."""
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
                        duplicates.append(
                            {
                                "id": drawer_id,
                                "wing": meta.get("wing", "?"),
                                "room": meta.get("room", "?"),
                                "similarity": similarity,
                                "content": doc[:200] + "..." if len(doc) > 200 else doc,
                            }
                        )
            return {
                "is_duplicate": len(duplicates) > 0,
                "matches": duplicates,
            }
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_traverse_graph(ctx: Context, start_room: str, max_hops: int = 2) -> dict:
        """[MEMPALACE] Walk the palace graph from a room. Shows connected ideas across wings."""
        col = _get_collection()
        if not col:
            return _no_palace()
        return traverse(start_room, col=col, max_hops=max_hops)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_find_tunnels(ctx: Context, wing_a: str | None = None, wing_b: str | None = None) -> dict:
        """[MEMPALACE] Find rooms that bridge two wings — the hallways connecting different domains."""
        col = _get_collection()
        if not col:
            return _no_palace()
        return find_tunnels(wing_a, wing_b, col=col)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_graph_stats(ctx: Context) -> dict:
        """[MEMPALACE] Palace graph overview: total rooms, tunnel connections, edges between wings."""
        col = _get_collection()
        if not col:
            return _no_palace()
        return graph_stats(col=col)

    # ── KNOWLEDGE GRAPH TOOLS ────────────────────────────────────

    # KG needs its own instance per server to avoid shared state
    kg = KnowledgeGraph(db_path=os.path.join(settings.db_path, "knowledge_graph.sqlite3"))

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_query(
        ctx: Context,
        entity: str,
        as_of: str | None = None,
        direction: str = "both",
        active_only: bool = False,
    ) -> dict:
        """[MEMPALACE] Query the knowledge graph for an entity's relationships."""
        results = kg.query_entity(entity, as_of=as_of, direction=direction, active_only=active_only)
        return {"entity": entity, "as_of": as_of, "facts": results, "count": len(results)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_kg_add(
        ctx: Context,
        subject: str,
        predicate: str,
        object: str,
        valid_from: str | None = None,
        source_closet: str | None = None,
    ) -> dict:
        """[MEMPALACE] Add a fact to the knowledge graph. Subject → predicate → object with optional time window."""
        try:
            subject = sanitize_name(subject, "subject")
            predicate = sanitize_name(predicate, "predicate")
            object = sanitize_name(object, "object")
        except ValueError as e:
            return {"success": False, "error": str(e)}

        _wal_log_async(
            "kg_add",
            {
                "subject": subject,
                "predicate": predicate,
                "object": object,
                "valid_from": valid_from,
                "source_closet": source_closet,
            },
            wal_file=_get_wal_path(settings.wal_dir),
        )
        triple_id = kg.add_triple(
            subject, predicate, object, valid_from=valid_from, source_closet=source_closet
        )
        return {"success": True, "triple_id": triple_id, "fact": f"{subject} → {predicate} → {object}"}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_kg_invalidate(
        ctx: Context,
        subject: str,
        predicate: str,
        object: str,
        ended: str | None = None,
    ) -> dict:
        """[MEMPALACE] Mark a fact as no longer true."""
        _wal_log_async(
            "kg_invalidate",
            {"subject": subject, "predicate": predicate, "object": object, "ended": ended},
            wal_file=_get_wal_path(settings.wal_dir),
        )
        kg.invalidate(subject, predicate, object, ended=ended)
        return {
            "success": True,
            "fact": f"{subject} → {predicate} → {object}",
            "ended": ended or "today",
        }

    @server.tool(timeout=settings.timeout_write)
    def mempalace_kg_supersede(
        ctx: Context,
        subject: str,
        predicate: str,
        old_value: str,
        new_value: str,
        agent_id: str = "unknown",
        source_closet: str | None = None,
    ) -> dict:
        """[MEMPALACE] Atomically supersede a fact: invalidate old, add new. Use when facts change."""
        try:
            subject = sanitize_name(subject, "subject")
            predicate = sanitize_name(predicate, "predicate")
        except ValueError as e:
            return {"success": False, "error": str(e)}
        _wal_log_async(
            "kg_supersede",
            {"subject": subject, "predicate": predicate, "old_value": old_value, "new_value": new_value, "agent_id": agent_id},
            wal_file=_get_wal_path(settings.wal_dir),
        )
        result = kg.supersede_triple(subject, predicate, old_value, new_value, agent_id=agent_id, source_closet=source_closet)
        return {"success": True, **result}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_timeline(ctx: Context, entity: str | None = None) -> dict:
        """[MEMPALACE] Chronological timeline of facts."""
        results = kg.timeline(entity)
        return {"entity": entity or "all", "timeline": results, "count": len(results)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_stats(ctx: Context) -> dict:
        """[MEMPALACE] Knowledge graph overview: entities, triples, current vs expired facts."""
        return kg.stats()

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_history(ctx: Context, subject: str, predicate: str) -> dict:
        """[MEMPALACE] Get full audit trail for a fact — all historical versions."""
        history = kg.get_triple_history(subject, predicate)
        return {
            "subject": subject,
            "predicate": predicate,
            "history": history,
            "versions": len(history),
            "current": next((h for h in history if h["current"]), None),
        }

    # ── WRITE TOOLS ──────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_add_drawer(
        ctx: Context,
        wing: str,
        room: str,
        content: str,
        source_file: str | None = None,
        added_by: str = "mcp",
    ) -> dict:
        """[MEMPALACE] File verbatim content into the palace. Checks for duplicates first."""
        try:
            wing = sanitize_name(wing, "wing")
            room = sanitize_name(room, "room")
            content = sanitize_content(content)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        col = _get_collection(create=True)
        if not col:
            return _no_palace()

        drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content[:100]).encode()).hexdigest()[:24]}"

        _wal_log_async(
            "add_drawer",
            {
                "drawer_id": drawer_id,
                "wing": wing,
                "room": room,
                "added_by": added_by,
                "content_length": len(content),
                "content_preview": content[:200],
            },
            wal_file=_get_wal_path(settings.wal_dir),
        )

        try:
            existing = col.get(ids=[drawer_id])
            if existing and existing["ids"]:
                return {"success": True, "reason": "already_exists", "drawer_id": drawer_id}
        except Exception:
            pass

        # PART 3b: MemoryGuard — block writes when memory pressure is critical
        if _memory_guard is not None:
            try:
                if _memory_guard.should_pause_writes():
                    reason = f"memory pressure: {_memory_guard.pressure.value} ({_memory_guard.used_ratio:.0%} used)"
                    logger.warning("memory_guard blocked add_drawer: %s", reason)
                    return {"error": f"Write blocked: {reason}", "blocked_by": "memory_guard", "pressure": _memory_guard.pressure.value}
            except Exception as e:
                logger.debug("memory_guard check failed, allowing: %s", e)
                # Fail open

        # PART 4: Auto entity extraction from content (max 20 most frequent)
        entities = []
        try:
            candidates = extract_candidates(content)
            if candidates:
                sorted_entities = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
                entities = [name for name, _ in sorted_entities[:20]]
        except Exception:
            pass  # Entity extraction is best-effort

        # PART 4b: General fact extraction (background, complementary to entity extraction)
        def _extract_general_facts(text: str, drawer_id: str):
            try:
                from .general_extractor import extract_memories
                facts = extract_memories(text)
                for fact in (facts or [])[:10]:
                    fact_text = str(fact.get("content", "")) if isinstance(fact, dict) else str(fact)
                    if fact_text:
                        logger.debug("general fact: %s from drawer %s", fact_text[:50], drawer_id)
            except (ImportError, Exception) as e:
                logger.debug("general_extractor skipped: %s", e)

        _bg_executor.submit(_extract_general_facts, content, drawer_id)

        try:
            col.upsert(
                ids=[drawer_id],
                documents=[content],
                metadatas=[
                    {
                        "wing": wing,
                        "room": room,
                        "source_file": source_file or "",
                        "chunk_index": 0,
                        "added_by": added_by,
                        "agent_id": added_by,
                        "entities": json.dumps(entities) if entities else "",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "origin_type": "observation",
                        "is_latest": True,
                        "supersedes_id": "",
                    }
                ],
            )
            logger.info(f"Filed drawer: {drawer_id} → {wing}/{room}")
            invalidate_query_cache()
            _invalidate_status_cache()

            return {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_delete_drawer(ctx: Context, drawer_id: str) -> dict:
        """[MEMPALACE] Delete a drawer by ID. Irreversible."""
        col = _get_collection()
        if not col:
            return _no_palace()

        existing = col.get(ids=[drawer_id])
        if not existing["ids"]:
            return {"success": False, "error": f"Drawer not found: {drawer_id}"}

        deleted_content = existing.get("documents", [""])[0] if existing.get("documents") else ""
        deleted_meta = existing.get("metadatas", [{}])[0] if existing.get("metadatas") else {}
        _wal_log_async(
            "delete_drawer",
            {
                "drawer_id": drawer_id,
                "deleted_meta": deleted_meta,
                "content_preview": deleted_content[:200],
            },
            wal_file=_get_wal_path(settings.wal_dir),
        )

        try:
            col.delete(ids=[drawer_id])
            invalidate_query_cache()
            _invalidate_status_cache()
            logger.info(f"Deleted drawer: {drawer_id}")
            return {"success": True, "drawer_id": drawer_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # TODO F176: diary entries could benefit from WriteCoalescer batch coalescing
    # similar to LanceDB backend — coalesce multiple diary writes in time window
    @server.tool(timeout=settings.timeout_write)
    def mempalace_diary_write(
        ctx: Context,
        agent_name: str,
        entry: str,
        topic: str = "general",
    ) -> dict:
        """[MEMPALACE] Write to your personal agent diary in AAAK format."""
        try:
            agent_name = sanitize_name(agent_name, "agent_name")
            entry = sanitize_content(entry)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        wing = f"wing_{agent_name.lower().replace(' ', '_')}"
        room = "diary"
        col = _get_collection(create=True)
        if not col:
            return _no_palace()

        now = datetime.now()
        entry_id = f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S')}_{hashlib.sha256(entry[:50].encode()).hexdigest()[:12]}"

        _wal_log_async(
            "diary_write",
            {
                "agent_name": agent_name,
                "topic": topic,
                "entry_id": entry_id,
                "entry_preview": entry[:200],
            },
            wal_file=_get_wal_path(settings.wal_dir),
        )

        try:
            col.upsert(
                ids=[entry_id],
                documents=[entry],
                metadatas=[
                    {
                        "wing": wing,
                        "room": room,
                        "source_file": f"diary://{agent_name}/{now.strftime('%Y-%m-%d')}",
                        "added_by": agent_name,
                        "agent_id": agent_name,
                        "topic": topic,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "origin_type": "diary_entry",
                        "is_latest": True,
                        "supersedes_id": "",
                        "chunk_index": 0,
                    }
                ],
            )
            logger.info(f"Diary entry: {entry_id} → {wing}/diary/{topic}")
            invalidate_query_cache()
            _invalidate_status_cache()
            return {
                "success": True,
                "entry_id": entry_id,
                "agent": agent_name,
                "topic": topic,
                "timestamp": now.isoformat(),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_diary_read(ctx: Context, agent_name: str, last_n: int = 10) -> dict:
        """[MEMPALACE] Read your recent diary entries (in AAAK)."""
        wing = f"wing_{agent_name.lower().replace(' ', '_')}"
        col = _get_collection()
        if not col:
            return _no_palace()

        try:
            # Iterative fetch — no hard 10k cap, processes ALL diary entries
            entries = []
            try:
                _BATCH = 500
                offset = 0
                while True:
                    batch = col.get(
                        where={"$and": [{"wing": wing}, {"room": "diary"}]},
                        include=["documents", "metadatas"],
                        limit=_BATCH,
                        offset=offset,
                    )
                    docs = batch.get("documents", [])
                    metas = batch.get("metadatas", [])
                    if not docs:
                        break
                    for doc, meta in zip(docs, metas):
                        entries.append({
                            "date": meta.get("date", ""),
                            "timestamp": meta.get("timestamp", ""),
                            "topic": meta.get("topic", ""),
                            "content": doc,
                        })
                    if len(docs) < _BATCH:
                        break
                    offset += len(docs)
            except Exception:
                pass

            if not entries:
                return {"agent": agent_name, "entries": [], "message": "No diary entries yet."}

            entries.sort(key=lambda x: x["timestamp"], reverse=True)
            entries = entries[:last_n]

            return {
                "agent": agent_name,
                "entries": entries,
                "total": len(entries),
                "showing": len(entries),
            }
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_embed)
    def mempalace_project_context(ctx: Context, project_path: str, limit: int = 10) -> dict:
        """[MEMPALACE] Query memories filtered by project_path and return formatted context."""
        col = _get_collection()
        if not col:
            return _no_palace()

        try:
            all_results = col.query(
                query_texts=[project_path],
                n_results=limit * 3,
                include=["documents", "metadatas", "distances"],
            )
            memories = []
            if all_results["ids"] and all_results["ids"][0]:
                for i, drawer_id in enumerate(all_results["ids"][0]):
                    meta = all_results["metadatas"][0][i]
                    doc = all_results["documents"][0][i]
                    dist = all_results["distances"][0][i]
                    # Filter by source_file containing project_path or wing containing project slug
                    project_slug = Path(project_path).name.lower().replace("-", "_")
                    source_match = project_path in meta.get("source_file", "")
                    wing_match = project_slug in meta.get("wing", "").lower()
                    if source_match or wing_match:
                        memories.append({
                            "id": drawer_id,
                            "wing": meta.get("wing", "?"),
                            "room": meta.get("room", "?"),
                            "similarity": round(1 - dist, 3),
                            "content": doc,
                        })
            memories = memories[:limit]
            return {
                "project_path": project_path,
                "memories": memories,
                "count": len(memories),
            }
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_remember_code(
        ctx: Context,
        code: str,
        description: str,
        wing: str,
        room: str,
        source_file: str | None = None,
        added_by: str = "mcp",
        language: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        symbol_name: str | None = None,
    ) -> dict:
        """[MEMPALACE] Store code with description, separating embedding from storage for better semantic search.

        code: the source code to store
        description: semantic description of what the code does
        wing/room: namespace for this memory
        source_file: optional path to source file
        language: optional programming language (auto-detected from source_file if not provided)
        line_start/line_end: optional line range in source file
        symbol_name: optional function/class name this code represents
        """
        try:
            wing = sanitize_name(wing, "wing")
            room = sanitize_name(room, "room")
            code = sanitize_content(code)
            description = sanitize_content(description)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        col = _get_collection(create=True)
        if not col:
            return _no_palace()

        drawer_id = f"code_{wing}_{room}_{hashlib.sha256((wing + room + description[:100]).encode()).hexdigest()[:24]}"

        _wal_log_async(
            "remember_code",
            {
                "drawer_id": drawer_id,
                "wing": wing,
                "room": room,
                "added_by": added_by,
                "code_length": len(code),
                "description_preview": description[:200],
            },
            wal_file=_get_wal_path(settings.wal_dir),
        )

        try:
            existing = col.get(ids=[drawer_id])
            if existing and existing["ids"]:
                return {"success": True, "reason": "already_exists", "drawer_id": drawer_id}

            # Auto entity extraction from code+description (max 20 most frequent)
            entities = []
            try:
                combined_text = f"{description} {code}"
                candidates = extract_candidates(combined_text)
                if candidates:
                    sorted_entities = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
                    entities = [name for name, _ in sorted_entities[:20]]
            except Exception:
                pass  # Entity extraction is best-effort

            code_stored = code[:2000]
            was_truncated = len(code) > 2000

            # Auto-detect language from source_file extension if not provided
            if not language and source_file:
                ext = Path(source_file).suffix.lower()
                from .miner import LANGUAGE_MAP
                language = LANGUAGE_MAP.get(ext, "Text")

            col.upsert(
                ids=[drawer_id],
                documents=[f"{description}\n\n```\n{code_stored}\n```"],
                metadatas=[{
                    "wing": wing,
                    "room": room,
                    "source_file": source_file or "",
                    "chunk_index": 0,
                    "added_by": added_by,
                    "agent_id": added_by,
                    "description": description,
                    "entities": json.dumps(entities) if entities else "",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "origin_type": "code_memory",
                    "is_latest": True,
                    "supersedes_id": "",
                    # Code-aware fields
                    "language": language or "",
                    "line_start": line_start or 0,
                    "line_end": line_end or 0,
                    "symbol_name": symbol_name or "",
                    "chunk_kind": "code_block",
                }],
            )
            logger.info(f"Remembered code: {drawer_id} → {wing}/{room}")
            invalidate_query_cache()
            _invalidate_status_cache()
            return {
                "success": True,
                "drawer_id": drawer_id,
                "wing": wing,
                "room": room,
                "code_truncated": was_truncated,
                "original_length": len(code),
                "stored_length": len(code_stored),
                "language": language,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool(timeout=settings.timeout_embed)
    def mempalace_consolidate(
        ctx: Context,
        topic: str,
        merge: bool = False,
        threshold: float = 0.85,
    ) -> dict:
        """[MEMPALACE] Find and optionally merge duplicate memories by topic."""
        col = _get_collection()
        if not col:
            return _no_palace()

        try:
            results = col.query(
                query_texts=[topic],
                n_results=50,
                include=["documents", "metadatas", "distances"],
            )

            if not results["ids"] or not results["ids"][0]:
                return {"topic": topic, "duplicates": [], "merged": 0}

            duplicates = []
            seen = set()

            for i, drawer_id in enumerate(results["ids"][0]):
                if drawer_id in seen:
                    continue

                dist = results["distances"][0][i]
                similarity = round(1 - dist, 3)

                if similarity >= threshold:
                    doc = results["documents"][0][i]
                    meta = results["metadatas"][0][i]
                    duplicates.append({
                        "id": drawer_id,
                        "wing": meta.get("wing", "?"),
                        "room": meta.get("room", "?"),
                        "similarity": similarity,
                        "content": doc[:300] + "..." if len(doc) > 300 else doc,
                    })
                    seen.add(drawer_id)

            merged_count = 0
            if merge and len(duplicates) > 1:
                # Sort by timestamp descending — newest as keeper
                duplicates_with_ts = []
                for dup in duplicates:
                    try:
                        raw = col.get(ids=[dup["id"]], include=["metadatas"])
                        ts = raw["metadatas"][0].get("timestamp", "") if raw["metadatas"] else ""
                    except Exception:
                        ts = ""
                    duplicates_with_ts.append({**dup, "_timestamp": ts})
                duplicates_with_ts.sort(key=lambda x: x["_timestamp"], reverse=True)
                duplicates = duplicates_with_ts
                keeper = duplicates[0]
                to_remove = duplicates[1:]

                for dup in to_remove:
                    try:
                        _wal_log_async(
                            "consolidate_delete",
                            {"deleted_id": dup["id"], "topic": topic, "keeper_id": keeper["id"]},
                            wal_file=_get_wal_path(settings.wal_dir),
                        )
                        col.delete(ids=[dup["id"]])
                        merged_count += 1
                    except Exception:
                        pass

                logger.info(f"Consolidated {merged_count} duplicate memories for topic: {topic}")
                invalidate_query_cache()
                _invalidate_status_cache()

            return {
                "topic": topic,
                "duplicates": duplicates,
                "merged": merged_count if merge else None,
                "total_found": len(duplicates),
            }
        except Exception as e:
            return {"error": str(e)}

    # ── CODE SEARCH TOOLS ─────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_search_code(
        ctx: Context,
        query: str,
        language: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        limit: int = 10,
    ) -> dict:
        """[MEMPALACE] Search code chunks with language/symbol/path filters. Use for code-aware retrieval.
        Automatically detects code-like queries and routes to the code-optimized search path.
        language: filter by language (e.g. "Python", "JavaScript", "Go")
        symbol_name: filter by exact function/class name
        file_path: filter by source file path substring
        """
        return await code_search_async(
            query=query,
            palace_path=settings.db_path,
            n_results=limit,
            language=language,
            symbol_name=symbol_name,
            file_path=file_path,
        )

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_auto_search(
        ctx: Context,
        query: str,
        limit: int = 10,
    ) -> dict:
        """[MEMPALACE] Auto-detect query type and route to optimal search path.
        Code queries → code_search (vector + FTS5)
        Prose queries → hybrid_search (vector + BM25 + KG)

        This is the recommended entry point for Claude Code — it handles routing automatically.
        """
        return await code_search_async(
            query=query,
            palace_path=settings.db_path,
            n_results=limit,
        ) if is_code_query(query) else await hybrid_search_async(
            query=query,
            palace_path=settings.db_path,
            n_results=limit,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_file_context(
        ctx: Context,
        file_path: str,
        line_start: int | None = None,
        line_end: int | None = None,
        context_lines: int = 5,
    ) -> dict:
        """[MEMPALACE] Read a file slice directly from disk — no DB needed.
        Returns lines in the specified range with context.

        file_path: absolute path to the file
        line_start: starting line (1-based), None = beginning
        line_end: ending line (1-based), None = end of file
        context_lines: number of extra lines to include around the range
        """
        p = Path(file_path).expanduser().resolve()
        if not p.exists():
            return {"error": f"File not found: {file_path}"}

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"error": str(e)}

        lines = content.split("\n")
        n = len(lines)

        start = max(0, (line_start or 1) - 1 - context_lines)
        end = min(n, (line_end or n) + context_lines)

        slice_lines = lines[start:end]
        has_more_before = line_start is not None and line_start > 1 + context_lines
        has_more_after = line_end is not None and line_end < n - context_lines

        return {
            "file_path": str(p),
            "total_lines": n,
            "range_start": start + 1,
            "range_end": end,
            "has_more_before": has_more_before,
            "has_more_after": has_more_after,
            "lines": [
                {"line_num": start + i + 1, "text": line}
                for i, line in enumerate(slice_lines)
            ],
        }

    @server.tool(timeout=settings.timeout_read)
    def mempalace_project_context(
        ctx: Context,
        project_path: str,
        query: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> dict:
        """[MEMPALACE] Get project-level code context: find all code chunks from files in a project directory.
        Optionally filter by language or search query.

        project_path: directory path to search within
        language: optional language filter
        query: optional semantic search query
        """
        col = _get_collection()
        if not col:
            return _no_palace()

        matched = []
        try:
            # Use col.query() to retrieve docs. ChromaDB doesn't support substring
            # matching in where filters, so we fetch results and filter in Python.
            # Avoid is_latest filter since seeded fixtures don't set it (assume all are latest)
            n_fetch = min(limit * 4, 200)
            if query:
                # Semantic search
                where = {}
                if language:
                    where["language"] = language

                q_result = col.query(
                    query_texts=[query],
                    n_results=n_fetch,
                    where=where if where else None,
                    include=["documents", "metadatas"],
                )
                docs = q_result.get("documents", [[]])[0] or []
                metas = q_result.get("metadatas", [[]])[0] or []
                for i, doc in enumerate(docs):
                    meta = metas[i] if i < len(metas) else {}
                    sf = meta.get("source_file", "")
                    if sf and project_path in sf:
                        matched.append({
                            "source_file": sf,
                            "language": meta.get("language", ""),
                            "line_start": meta.get("line_start", 0),
                            "line_end": meta.get("line_end", 0),
                            "symbol_name": meta.get("symbol_name", ""),
                            "chunk_kind": meta.get("chunk_kind", ""),
                            "doc": doc,
                        })
                        if len(matched) >= limit:
                            break
            else:
                # No query: use empty string query to get all, then filter
                where = {}
                if language:
                    where["language"] = language

                q_result = col.query(
                    query_texts=[""],
                    n_results=n_fetch,
                    where=where if where else None,
                    include=["documents", "metadatas"],
                )
                docs = q_result.get("documents", [[]])[0] or []
                metas = q_result.get("metadatas", [[]])[0] or []
                for i, doc in enumerate(docs):
                    meta = metas[i] if i < len(metas) else {}
                    sf = meta.get("source_file", "")
                    if sf and project_path in sf:
                        matched.append({
                            "source_file": sf,
                            "language": meta.get("language", ""),
                            "line_start": meta.get("line_start", 0),
                            "line_end": meta.get("line_end", 0),
                            "symbol_name": meta.get("symbol_name", ""),
                            "chunk_kind": meta.get("chunk_kind", ""),
                            "doc": doc,
                        })
                        if len(matched) >= limit:
                            break

            return {
                "project_path": project_path,
                "language": language,
                "query": query,
                "chunks": matched,
                "count": len(matched),
            }
        except Exception as e:
            return {"error": str(e), "project_path": project_path}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_export_claude_md(
        ctx: Context,
        wing: str | None = None,
        room: str | None = None,
        format: str = "markdown",
    ) -> dict:
        """[MEMPALACE] Export memories to CLAUDE.md format for use as project documentation."""
        col = _get_collection()
        if not col:
            return _no_palace()

        try:
            where = {}
            if wing:
                where["wing"] = wing
            if room:
                where["room"] = room

            # Iterative fetch — no hard 10k cap, processes ALL matching records
            memories = []
            _BATCH = 500
            offset = 0
            while True:
                kwargs = {"include": ["documents", "metadatas"], "limit": _BATCH, "offset": offset}
                if where:
                    kwargs["where"] = where
                batch = col.get(**kwargs)
                docs = batch.get("documents", [])
                metas = batch.get("metadatas", [])
                if not docs:
                    break
                for doc, meta in zip(docs, metas):
                    memories.append({
                        "wing": meta.get("wing", "unknown"),
                        "room": meta.get("room", "unknown"),
                        "content": doc,
                        "source_file": meta.get("source_file", ""),
                    })
                if len(docs) < _BATCH:
                    break
                offset += len(docs)

            if not memories:
                return {
                    "export": "",
                    "count": 0,
                    "message": "No memories found for the specified criteria.",
                }

            if format == "json":
                return {
                    "export": memories,
                    "count": len(memories),
                    "format": "json",
                }

            lines = [
                "# MemPalace Export",
                "",
                f"Exported at: {datetime.now().isoformat()}",
                f"Total memories: {len(memories)}",
                "",
            ]

            if wing:
                lines.append(f"## Wing: {wing}")
            if room:
                lines.append(f"### Room: {room}")

            for mem in memories:
                lines.append("")
                lines.append(f"### [{mem['wing']}] {mem['room']}")
                if mem['source_file']:
                    lines.append(f"*Source: {mem['source_file']}*")
                lines.append("")
                lines.append(mem['content'])
                lines.append("---")

            return {
                "export": "\n".join(lines),
                "count": len(memories),
                "format": "markdown",
            }
        except Exception as e:
            return {"error": str(e)}

    # ── SESSION COORDINATION TOOLS ──────────────────────────────────

    def _get_claims_manager():
        mgr = getattr(server, "_claims_manager", None)
        if mgr is None:
            return None
        return mgr

    def _get_handoff_manager():
        mgr = getattr(server, "_handoff_manager", None)
        if mgr is None:
            return None
        return mgr

    def _get_decision_tracker():
        mgr = getattr(server, "_decision_tracker", None)
        if mgr is None:
            return None
        return mgr

    @server.tool(timeout=settings.timeout_write)
    def mempalace_claim_path(
        ctx: Context,
        path: str,
        session_id: str,
        ttl_seconds: int = 600,
        note: str | None = None,
    ) -> dict:
        """[MEMPALACE] Claim a file path with TTL for mutual exclusion."""
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        payload = {"note": note, "path": path} if note else {"path": path}
        return mgr.claim("file", path, session_id, ttl_seconds=ttl_seconds, payload=payload)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_release_claim(
        ctx: Context,
        path: str,
        session_id: str,
    ) -> dict:
        """[MEMPALACE] Release a claim on a file path."""
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        return mgr.release_claim("file", path, session_id)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_claims(
        ctx: Context,
        session_id: str | None = None,
    ) -> dict:
        """[MEMPALACE] List active claims (all or for a session)."""
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        if session_id:
            claims = mgr.get_session_claims(session_id)
        else:
            claims = mgr.list_active_claims()
        return {"claims": claims, "count": len(claims)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_conflict_check(
        ctx: Context,
        path: str,
        session_id: str,
    ) -> dict:
        """[MEMPALACE] Check for conflicts before editing a file."""
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        return mgr.check_conflicts("file", path, session_id)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_push_handoff(
        ctx: Context,
        from_session_id: str,
        summary: str,
        touched_paths: list[str],
        blockers: list[str],
        next_steps: list[str],
        confidence: int,
        priority: str = "normal",
        to_session_id: str | None = None,
    ) -> dict:
        """[MEMPALACE] Create a cross-session handoff."""
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        return mgr.push_handoff(
            from_session_id=from_session_id,
            summary=summary,
            touched_paths=touched_paths,
            blockers=blockers,
            next_steps=next_steps,
            confidence=confidence,
            priority=priority,
            to_session_id=to_session_id,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_pull_handoffs(
        ctx: Context,
        session_id: str,
        status: str | None = None,
    ) -> dict:
        """[MEMPALACE] Get pending handoffs for a session."""
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        handoffs = mgr.pull_handoffs(session_id=session_id, status=status)
        return {"handoffs": handoffs, "count": len(handoffs)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_accept_handoff(
        ctx: Context,
        handoff_id: str,
        session_id: str,
    ) -> dict:
        """[MEMPALACE] Accept a handoff."""
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        return mgr.accept_handoff(handoff_id, session_id)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_complete_handoff(
        ctx: Context,
        handoff_id: str,
        session_id: str,
    ) -> dict:
        """[MEMPALACE] Mark a handoff as completed."""
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        return mgr.complete_handoff(handoff_id, session_id)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_wakeup_context(
        ctx: Context,
        session_id: str,
        project_root: str | None = None,
    ) -> dict:
        """[MEMPALACE] Get full wakeup bundle for session resume/takeover."""
        try:
            result = build_wakeup_context(
                session_id=session_id,
                project_root=project_root,
                palace_path=config.palace_path,
            )
            return result
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_capture_decision(
        ctx: Context,
        session_id: str,
        decision: str,
        rationale: str,
        alternatives: list[str],
        category: str,
        confidence: int,
    ) -> dict:
        """[MEMPALACE] Store an architectural decision."""
        mgr = _get_decision_tracker()
        if mgr is None:
            return {"error": "session coordination not available"}

        return mgr.capture_decision(
            session_id=session_id,
            decision_text=decision,
            rationale=rationale,
            alternatives=alternatives,
            category=category,
            confidence=confidence,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_decisions(
        ctx: Context,
        session_id: str | None = None,
        category: str | None = None,
    ) -> dict:
        """[MEMPALACE] Query stored decisions."""
        mgr = _get_decision_tracker()
        if mgr is None:
            return {"error": "session coordination not available"}

        decisions = mgr.list_decisions(session_id=session_id, category=category)
        return {"decisions": decisions, "count": len(decisions)}


# ═══════════════════════════════════════════════════════════════════
# HTTP TRANSPORT
# ═══════════════════════════════════════════════════════════════════

def serve_http(host: str = "127.0.0.1", port: int = 8765, server: FastMCP | None = None) -> None:
    """Run MemPalace FastMCP server over HTTP using Starlette + Uvicorn."""
    try:
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        import uvicorn
    except ImportError:
        logger.error("HTTP transport requires starlette and uvicorn.")
        sys.exit(1)

    if server is None:
        server = create_server(shared_server_mode=True)

    async def http_handle(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")

        if request.method == "GET":
            return JSONResponse({"error": "SSE not implemented. Use POST with application/json."}, status_code=400)

        if request.method == "POST" and "application/json" in content_type:
            try:
                body = await request.body()
                request_data = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JSONResponse({"error": "Invalid JSON"}, status_code=400)

            response_data = server.handle_request(request_data)
            if response_data is None:
                return Response(status_code=204)
            return JSONResponse(response_data)

        return JSONResponse({"error": "Unsupported media type"}, status_code=415)

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "transport": "http"})

    routes = [
        Route("/mcp", http_handle, methods=["GET", "POST"]),
        Route("/health", health, methods=["GET"]),
    ]

    app = Starlette(routes=routes)
    logger.info("MemPalace FastMCP HTTP server starting at http://%s:%d/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


# ═══════════════════════════════════════════════════════════════════
# MAIN — CLI argument parsing + singleton creation
# ═══════════════════════════════════════════════════════════════════

def _parse_args():
    parser = argparse.ArgumentParser(description="MemPalace FastMCP Server")
    parser.add_argument(
        "--palace",
        metavar="PATH",
        help="Path to the palace directory (overrides config file and env var)",
    )
    args, unknown = parser.parse_known_args()
    if unknown:
        logger.debug("Ignoring unknown args: %s", unknown)
    return args


if __name__ == "__main__":
    _args = _parse_args()

    # Override settings from CLI
    if _args.palace:
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(_args.palace)

    # Create singleton from default settings (production use)
    mcp = create_server()

    if settings.transport == "http":
        # Native FastMCP streamable-http transport
        mcp.run(
            transport="streamable-http",
            host=settings.host,
            port=settings.port,
        )
    else:
        mcp.run()