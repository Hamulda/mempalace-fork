#!/usr/bin/env python3
"""
MemPalace FastMCP Server — read/write palace access for Claude Code
===============================================================
Migrated from low-level MCP SDK to FastMCP v3.
Middleware: ResponseCaching + CacheInvalidation + EmbedCircuitBreaker

Install: claude mcp add mempalace -- python -m mempalace.fastmcp_server [--palace /path/to/palace]
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

from .config import MempalaceConfig, sanitize_name, sanitize_content
from .middleware import (
    get_caching_middleware,
    get_cache_invalidation_middleware,
    get_embed_circuit_middleware,
    get_session_tracking_middleware,
)
from .settings import settings
from .version import __version__
from .searcher import search_memories
from .palace_graph import traverse, find_tunnels, graph_stats
from .knowledge_graph import KnowledgeGraph
from .backends import get_backend

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("mempalace_mcp")


# ═══════════════════════════════════════════════════════════════════
# MIDDLEWARE STACK
# ═══════════════════════════════════════════════════════════════════

_caching_mw = get_caching_middleware()
_cache_inv_mw = get_cache_invalidation_middleware()
_embed_cb_mw = get_embed_circuit_middleware()


# ═══════════════════════════════════════════════════════════════════
# INIT & CONFIG
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


_args = _parse_args()

if _args.palace:
    os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(_args.palace)

_config = MempalaceConfig()
if _args.palace:
    _kg = KnowledgeGraph(db_path=os.path.join(_config.palace_path, "knowledge_graph.sqlite3"))
else:
    _kg = KnowledgeGraph()


# ═══════════════════════════════════════════════════════════════════
# WRITE-AHEAD LOG
# ═══════════════════════════════════════════════════════════════════

_WAL_DIR = Path(os.path.expanduser("~/.mempalace/wal"))
_WAL_DIR.mkdir(parents=True, exist_ok=True)
try:
    _WAL_DIR.chmod(0o700)
except (OSError, NotImplementedError):
    pass
_WAL_FILE = _WAL_DIR / "write_log.jsonl"


def _wal_log(operation: str, params: dict, result: dict = None):
    """Append a write operation to the write-ahead log."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation,
        "params": params,
        "result": result,
    }
    try:
        with open(_WAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        try:
            _WAL_FILE.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
    except Exception as e:
        logger.error(f"WAL write failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# COLLECTION HELPERS
# ═══════════════════════════════════════════════════════════════════

_collection_cache = None


def _get_collection(create=False):
    """Return the palace collection using the configured backend."""
    global _collection_cache
    if _collection_cache is not None:
        return _collection_cache

    try:
        backend = get_backend(_config.backend)
        _collection_cache = backend.get_collection(
            _config.palace_path, _config.collection_name, create=create
        )
        return _collection_cache
    except Exception:
        return None


def _no_palace():
    return {
        "error": "No palace found",
        "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
    }


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
  WINGS: wing_user, wing_agent, wing_team, wing_code, wing_myproject, wing_hardware, wing_ue5, wing_ai_research.
  ROOMS: Hyphenated slugs representing named ideas (e.g., chromadb-setup, gpu-pricing).

EXAMPLE:
  FAM: ALC→♡JOR | 2D(kids): RIL(18,sports) MAX(11,chess+swimming) | BEN(contributor)

Read AAAK naturally — expand codes mentally, treat *markers* as emotional context.
When WRITING AAAK: use entity codes, mark emotions, keep structure tight."""


# ═══════════════════════════════════════════════════════════════════
# MCP SERVER INSTANCE
# ═══════════════════════════════════════════════════════════════════

mcp = FastMCP("MemPalace")
# TODO(Sprint F155): Refactor to factory pattern for test isolation.
# Current issue: `mcp` is a module-level singleton initialized with real
# palace config at import time. FastMCP Client(transport=mcp) uses this
# pre-initialized server, which reads from the REAL palace, not test fixtures.
# Fix: create_server(settings) factory that returns a new FastMCP instance.
mcp.add_middleware(get_session_tracking_middleware())
mcp.add_middleware(_caching_mw)
mcp.add_middleware(_cache_inv_mw)
mcp.add_middleware(_embed_cb_mw)


# ═══════════════════════════════════════════════════════════════════
# READ TOOLS (cached via ResponseCachingMiddleware)
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
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
        "palace_path": _config.palace_path,
        "protocol": PALACE_PROTOCOL,
        "aaak_dialect": AAAK_SPEC,
    }


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def mempalace_get_aaak_spec(ctx: Context) -> dict:
    """[MEMPALACE] Get the AAAK dialect specification — the compressed memory format MemPalace uses."""
    return {"aaak_spec": AAAK_SPEC}


@mcp.tool()
def mempalace_search(
    ctx: Context,
    query: str,
    limit: int = 5,
    wing: str | None = None,
    room: str | None = None,
) -> dict:
    """[MEMPALACE] Semantic search. Returns verbatim drawer content with similarity scores."""
    return search_memories(
        query,
        palace_path=_config.palace_path,
        wing=wing,
        room=room,
        n_results=limit,
    )


@mcp.tool()
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


@mcp.tool()
def mempalace_traverse_graph(ctx: Context, start_room: str, max_hops: int = 2) -> dict:
    """[MEMPALACE] Walk the palace graph from a room. Shows connected ideas across wings."""
    col = _get_collection()
    if not col:
        return _no_palace()
    return traverse(start_room, col=col, max_hops=max_hops)


@mcp.tool()
def mempalace_find_tunnels(ctx: Context, wing_a: str | None = None, wing_b: str | None = None) -> dict:
    """[MEMPALACE] Find rooms that bridge two wings — the hallways connecting different domains."""
    col = _get_collection()
    if not col:
        return _no_palace()
    return find_tunnels(wing_a, wing_b, col=col)


@mcp.tool()
def mempalace_graph_stats(ctx: Context) -> dict:
    """[MEMPALACE] Palace graph overview: total rooms, tunnel connections, edges between wings."""
    col = _get_collection()
    if not col:
        return _no_palace()
    return graph_stats(col=col)


# ═══════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH TOOLS
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
def mempalace_kg_query(
    ctx: Context,
    entity: str,
    as_of: str | None = None,
    direction: str = "both",
) -> dict:
    """[MEMPALACE] Query the knowledge graph for an entity's relationships."""
    results = _kg.query_entity(entity, as_of=as_of, direction=direction)
    return {"entity": entity, "as_of": as_of, "facts": results, "count": len(results)}


@mcp.tool()
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
    )
    triple_id = _kg.add_triple(
        subject, predicate, object, valid_from=valid_from, source_closet=source_closet
    )
    return {"success": True, "triple_id": triple_id, "fact": f"{subject} → {predicate} → {object}"}


@mcp.tool()
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
    )
    _kg.invalidate(subject, predicate, object, ended=ended)
    return {
        "success": True,
        "fact": f"{subject} → {predicate} → {object}",
        "ended": ended or "today",
    }


@mcp.tool()
def mempalace_kg_timeline(ctx: Context, entity: str | None = None) -> dict:
    """[MEMPALACE] Chronological timeline of facts."""
    results = _kg.timeline(entity)
    return {"entity": entity or "all", "timeline": results, "count": len(results)}


@mcp.tool()
def mempalace_kg_stats(ctx: Context) -> dict:
    """[MEMPALACE] Knowledge graph overview: entities, triples, current vs expired facts."""
    return _kg.stats()


# ═══════════════════════════════════════════════════════════════════
# WRITE TOOLS (cache invalidation via CacheInvalidationMiddleware)
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
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
                }
            ],
        )
        logger.info(f"Filed drawer: {drawer_id} → {wing}/{room}")
        return {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
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
    )

    try:
        col.delete(ids=[drawer_id])
        logger.info(f"Deleted drawer: {drawer_id}")
        return {"success": True, "drawer_id": drawer_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
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
    )

    try:
        col.add(
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


@mcp.tool()
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


@mcp.tool()
def mempalace_project_context(ctx: Context, project_path: str, limit: int = 10) -> dict:
    """[MEMPALACE] Query memories filtered by project_path and return formatted context."""
    col = _get_collection()
    if not col:
        return _no_palace()

    try:
        results = col.query(
            query_texts=[project_path],
            n_results=limit,
            where={"project": project_path},
            include=["documents", "metadatas", "distances"],
        )

        memories = []
        if results["ids"] and results["ids"][0]:
            for i, drawer_id in enumerate(results["ids"][0]):
                doc = results["documents"][0][i]
                meta = results["metadatas"][0][i]
                dist = results["distances"][0][i]
                similarity = round(1 - dist, 3)
                memories.append({
                    "id": drawer_id,
                    "wing": meta.get("wing", "?"),
                    "room": meta.get("room", "?"),
                    "similarity": similarity,
                    "content": doc,
                })

        return {
            "project_path": project_path,
            "memories": memories,
            "count": len(memories),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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
            }],
        )
        logger.info(f"Remembered code: {drawer_id} → {wing}/{room}")
        return {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
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
            keeper = duplicates[0]
            to_remove = duplicates[1:]

            for dup in to_remove:
                try:
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


@mcp.tool()
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

def serve_http(host: str = "127.0.0.1", port: int = 8766) -> None:
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

            response_data = mcp.handle_request(request_data)
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
# MAIN
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Použij settings pro transport konfiguraci
    if settings.transport == "http":
        serve_http(host=settings.host, port=settings.port)
    else:
        mcp.run()  # stdio default — zachovat pro Claude Code
