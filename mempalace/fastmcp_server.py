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
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP, Context
from fastmcp.resources import DirectoryResource
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import MempalaceConfig, sanitize_name, sanitize_content
from .middleware import build_middleware_stack
from .settings import settings, MemPalaceSettings
from .version import __version__
from .searcher import search_memories, search_memories_async, hybrid_search_async
from .palace_graph import traverse, find_tunnels, graph_stats
from .knowledge_graph import KnowledgeGraph
from .backends import get_backend

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("mempalace_mcp")


# ═══════════════════════════════════════════════════════════════════
# WRITE-AHEAD LOG
# ═══════════════════════════════════════════════════════════════════

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

def create_server(settings: MemPalaceSettings | None = None) -> FastMCP:
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

    # ── READ TOOLS ────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_status(ctx: Context) -> dict:
        """[MEMPALACE] Palace overview — total drawers, wing and room counts."""
        col = _get_collection()
        if not col:
            return _no_palace()

        count = col.count()
        wings = {}
        rooms = {}
        try:
            all_meta = col.get(include=["metadatas"], limit=10000)["metadatas"]
            for m in all_meta:
                w = m.get("wing", "unknown")
                r = m.get("room", "unknown")
                wings[w] = wings.get(w, 0) + 1
                rooms[r] = rooms.get(r, 0) + 1
        except Exception:
            pass

        ctx.debug(f"status returning {count} drawers")
        return {
            "total_drawers": count,
            "wings": wings,
            "rooms": rooms,
            "palace_path": settings.db_path,
            "protocol": PALACE_PROTOCOL,
            "aaak_dialect": AAAK_SPEC,
        }

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_wings(ctx: Context) -> dict:
        """[MEMPALACE] List all wings with drawer counts."""
        col = _get_collection()
        if not col:
            return _no_palace()

        wings = {}
        try:
            all_meta = col.get(include=["metadatas"], limit=10000)["metadatas"]
            for m in all_meta:
                w = m.get("wing", "unknown")
                wings[w] = wings.get(w, 0) + 1
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
            kwargs = {"include": ["metadatas"], "limit": 10000}
            if wing:
                kwargs["where"] = {"wing": wing}
            all_meta = col.get(**kwargs)["metadatas"]
            for m in all_meta:
                r = m.get("room", "unknown")
                rooms[r] = rooms.get(r, 0) + 1
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
            all_meta = col.get(include=["metadatas"], limit=10000)["metadatas"]
            for m in all_meta:
                w = m.get("wing", "unknown")
                r = m.get("room", "unknown")
                if w not in taxonomy:
                    taxonomy[w] = {}
                taxonomy[w][r] = taxonomy[w].get(r, 0) + 1
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
    ) -> dict:
        """[MEMPALACE] Hybrid search: semantic (ChromaDB) + knowledge graph (SQLite) combined.
        use_kg=True adds entity relationship facts alongside vector matches.
        rerank: bool — if True, apply cross-encoder reranking (slower, better precision)."""
        return await hybrid_search_async(
            query=query, palace_path=settings.db_path, wing=wing, room=room,
            n_results=limit, use_kg=use_kg, rerank=rerank, agent_id=agent_id,
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

        _wal_log(
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
        _wal_log(
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
        _wal_log(
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

        _wal_log(
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
                        "filed_at": datetime.now().isoformat(),
                        "agent_id": added_by,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "origin_type": "observation",
                        "is_latest": True,
                        "supersedes_id": "",
                    }
                ],
            )
            logger.info(f"Filed drawer: {drawer_id} → {wing}/{room}")
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
        _wal_log(
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
            logger.info(f"Deleted drawer: {drawer_id}")
            return {"success": True, "drawer_id": drawer_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

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

        _wal_log(
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
                        "hall": "hall_diary",
                        "topic": topic,
                        "type": "diary_entry",
                        "agent": agent_name,
                        "filed_at": now.isoformat(),
                        "date": now.strftime('%Y-%m-%d'),
                        "agent_id": agent_name,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "origin_type": "diary_entry",
                        "is_latest": True,
                        "supersedes_id": "",
                    }
                ],
            )
            logger.info(f"Diary entry: {entry_id} → {wing}/diary/{topic}")
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
            results = col.get(
                where={"$and": [{"wing": wing}, {"room": "diary"}]},
                include=["documents", "metadatas"],
                limit=10000,
            )

            if not results["ids"]:
                return {"agent": agent_name, "entries": [], "message": "No diary entries yet."}

            entries = []
            for doc, meta in zip(results["documents"], results["metadatas"]):
                entries.append(
                    {
                        "date": meta.get("date", ""),
                        "timestamp": meta.get("filed_at", ""),
                        "topic": meta.get("topic", ""),
                        "content": doc,
                    }
                )

            entries.sort(key=lambda x: x["timestamp"], reverse=True)
            entries = entries[:last_n]

            return {
                "agent": agent_name,
                "entries": entries,
                "total": len(results["ids"]),
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
    ) -> dict:
        """[MEMPALACE] Store code with description, separating embedding from storage for better semantic search."""
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

        _wal_log(
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

            col.upsert(
                ids=[drawer_id],
                documents=[f"{description}\n\n```\n{code[:2000]}\n```"],
                metadatas=[{
                    "wing": wing,
                    "room": room,
                    "source_file": source_file or "",
                    "type": "code_memory",
                    "description": description,
                    "added_by": added_by,
                    "filed_at": datetime.now().isoformat(),
                    "agent_id": added_by,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "origin_type": "observation",
                    "is_latest": True,
                    "supersedes_id": "",
                }],
            )
            logger.info(f"Remembered code: {drawer_id} → {wing}/{room}")
            return {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room}
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
                # Sort by filed_at descending — newest as keeper
                duplicates_with_ts = []
                for dup in duplicates:
                    try:
                        raw = col.get(ids=[dup["id"]], include=["metadatas"])
                        ts = raw["metadatas"][0].get("filed_at", "") if raw["metadatas"] else ""
                    except Exception:
                        ts = ""
                    duplicates_with_ts.append({**dup, "_filed_at": ts})
                duplicates_with_ts.sort(key=lambda x: x["_filed_at"], reverse=True)
                duplicates = duplicates_with_ts
                keeper = duplicates[0]
                to_remove = duplicates[1:]

                for dup in to_remove:
                    try:
                        _wal_log(
                            "consolidate_delete",
                            {"deleted_id": dup["id"], "topic": topic, "keeper_id": keeper["id"]},
                            wal_file=_get_wal_path(settings.wal_dir),
                        )
                        col.delete(ids=[dup["id"]])
                        merged_count += 1
                    except Exception:
                        pass

                logger.info(f"Consolidated {merged_count} duplicate memories for topic: {topic}")

            return {
                "topic": topic,
                "duplicates": duplicates,
                "merged": merged_count if merge else None,
                "total_found": len(duplicates),
            }
        except Exception as e:
            return {"error": str(e)}

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

            kwargs = {"include": ["documents", "metadatas"], "limit": 10000}
            if where:
                kwargs["where"] = where

            results = col.get(**kwargs)

            if not results["ids"]:
                return {
                    "export": "",
                    "count": 0,
                    "message": "No memories found for the specified criteria.",
                }

            memories = []
            for doc, meta in zip(results["documents"], results["metadatas"]):
                memories.append({
                    "wing": meta.get("wing", "unknown"),
                    "room": meta.get("room", "unknown"),
                    "content": doc,
                    "source_file": meta.get("source_file", ""),
                })

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
        server = mcp

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