"""
Write tools: add_drawer, delete_drawer, diary_write, diary_read, consolidate.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from fastmcp import Context


def register_write_tools(server, backend, config, settings, memory_guard):
    """
    Register all write @mcp.tool() as closures over backend/config/kg.
    Called by factory._register_tools().
    """
    from ._infrastructure import wal_log_async, get_wal_path, bg_executor, invalidate_status_cache
    from ..searcher import invalidate_query_cache
    from ..entity_detector import extract_candidates
    from ..config import sanitize_name, sanitize_content

    def _get_collection(create=False):
        try:
            return backend.get_collection(
                settings.db_path, settings.effective_collection_name, create=create
            )
        except Exception:
            return None

    def _no_palace():
        return {"error": "No palace found", "hint": "Run: mempalace init <dir> && mempalace mine <dir>"}

    def _invalidate_status_cache():
        invalidate_status_cache()

    @server.tool(timeout=settings.timeout_write)
    def mempalace_add_drawer(
        ctx: Context,
        wing: str,
        room: str,
        content: str,
        source_file: str | None = None,
        added_by: str = "mcp",
    ) -> dict:
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

        wal_log_async(
            "add_drawer",
            {"drawer_id": drawer_id, "wing": wing, "room": room, "added_by": added_by,
             "content_length": len(content), "content_preview": content[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )

        try:
            existing = col.get(ids=[drawer_id])
            if existing and existing["ids"]:
                return {"success": True, "reason": "already_exists", "drawer_id": drawer_id}
        except Exception:
            pass

        if memory_guard is not None:
            try:
                if memory_guard.should_pause_writes():
                    reason = f"memory pressure: {memory_guard.pressure.value} ({memory_guard.used_ratio:.0%} used)"
                    return {"error": f"Write blocked: {reason}", "blocked_by": "memory_guard", "pressure": memory_guard.pressure.value}
            except Exception:
                pass  # Fail open

        entities = []
        try:
            candidates = extract_candidates(content)
            if candidates:
                sorted_entities = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
                entities = [name for name, _ in sorted_entities[:20]]
        except Exception:
            pass

        def _extract_general_facts(text: str, drawer_id: str):
            try:
                from ..general_extractor import extract_memories
                facts = extract_memories(text)
                for fact in (facts or [])[:10]:
                    fact_text = str(fact.get("content", "")) if isinstance(fact, dict) else str(fact)
                    if fact_text:
                        pass
            except (ImportError, Exception):
                pass

        bg_executor.submit(_extract_general_facts, content, drawer_id)

        try:
            col.upsert(
                ids=[drawer_id],
                documents=[content],
                metadatas=[{
                    "wing": wing, "room": room, "source_file": source_file or "", "chunk_index": 0,
                    "added_by": added_by, "agent_id": added_by,
                    "entities": json.dumps(entities) if entities else "",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "origin_type": "observation", "is_latest": True, "supersedes_id": "",
                }],
            )
            invalidate_query_cache()
            _invalidate_status_cache()
            return {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_delete_drawer(ctx: Context, drawer_id: str) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        existing = col.get(ids=[drawer_id])
        if not existing["ids"]:
            return {"success": False, "error": f"Drawer not found: {drawer_id}"}
        deleted_content = existing.get("documents", [""])[0] if existing.get("documents") else ""
        deleted_meta = existing.get("metadatas", [{}])[0] if existing.get("metadatas") else {}
        wal_log_async(
            "delete_drawer",
            {"drawer_id": drawer_id, "deleted_meta": deleted_meta, "content_preview": deleted_content[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )
        try:
            col.delete(ids=[drawer_id])
            invalidate_query_cache()
            _invalidate_status_cache()
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
        wal_log_async(
            "diary_write",
            {"agent_name": agent_name, "topic": topic, "entry_id": entry_id, "entry_preview": entry[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )
        try:
            col.upsert(
                ids=[entry_id],
                documents=[entry],
                metadatas=[{
                    "wing": wing, "room": room,
                    "source_file": f"diary://{agent_name}/{now.strftime('%Y-%m-%d')}",
                    "added_by": agent_name, "agent_id": agent_name, "topic": topic,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "origin_type": "diary_entry", "is_latest": True, "supersedes_id": "", "chunk_index": 0,
                }],
            )
            invalidate_query_cache()
            _invalidate_status_cache()
            return {"success": True, "entry_id": entry_id, "agent": agent_name, "topic": topic, "timestamp": now.isoformat()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_diary_read(ctx: Context, agent_name: str, last_n: int = 10) -> dict:
        wing = f"wing_{agent_name.lower().replace(' ', '_')}"
        col = _get_collection()
        if not col:
            return _no_palace()
        try:
            entries = []
            try:
                _BATCH = 500
                offset = 0
                while True:
                    batch = col.get(
                        where={"$and": [{"wing": wing}, {"room": "diary"}]},
                        include=["documents", "metadatas"],
                        limit=_BATCH, offset=offset,
                    )
                    docs = batch.get("documents", [])
                    metas = batch.get("metadatas", [])
                    if not docs:
                        break
                    for doc, meta in zip(docs, metas):
                        entries.append({
                            "date": meta.get("date", ""), "timestamp": meta.get("timestamp", ""),
                            "topic": meta.get("topic", ""), "content": doc,
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
            return {"agent": agent_name, "entries": entries, "total": len(entries), "showing": len(entries)}
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
        wal_log_async(
            "remember_code",
            {"drawer_id": drawer_id, "wing": wing, "room": room, "added_by": added_by,
             "code_length": len(code), "description_preview": description[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )
        try:
            existing = col.get(ids=[drawer_id])
            if existing and existing["ids"]:
                return {"success": True, "reason": "already_exists", "drawer_id": drawer_id}
            entities = []
            try:
                combined_text = f"{description} {code}"
                candidates = extract_candidates(combined_text)
                if candidates:
                    sorted_entities = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
                    entities = [name for name, _ in sorted_entities[:20]]
            except Exception:
                pass
            code_stored = code[:2000]
            was_truncated = len(code) > 2000
            if not language and source_file:
                ext = Path(source_file).suffix.lower()
                from ..miner import LANGUAGE_MAP
                language = LANGUAGE_MAP.get(ext, "Text")
            col.upsert(
                ids=[drawer_id],
                documents=[f"{description}\n\n```\n{code_stored}\n```"],
                metadatas=[{
                    "wing": wing, "room": room, "source_file": source_file or "", "chunk_index": 0,
                    "added_by": added_by, "agent_id": added_by, "description": description,
                    "entities": json.dumps(entities) if entities else "",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "origin_type": "code_memory", "is_latest": True, "supersedes_id": "",
                    "language": language or "", "line_start": line_start or 0, "line_end": line_end or 0,
                    "symbol_name": symbol_name or "", "chunk_kind": "code_block",
                }],
            )
            invalidate_query_cache()
            _invalidate_status_cache()
            return {
                "success": True, "drawer_id": drawer_id, "wing": wing, "room": room,
                "code_truncated": was_truncated, "original_length": len(code),
                "stored_length": len(code_stored), "language": language,
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
                        "id": drawer_id, "wing": meta.get("wing", "?"), "room": meta.get("room", "?"),
                        "similarity": similarity,
                        "content": doc[:300] + "..." if len(doc) > 300 else doc,
                    })
                    seen.add(drawer_id)
            merged_count = 0
            if merge and len(duplicates) > 1:
                duplicates_with_ts = []
                for dup in duplicates:
                    try:
                        raw = col.get(ids=[dup["id"]], include=["metadatas"])
                        ts = raw["metadatas"][0].get("timestamp", "") if raw["metadatas"] else ""
                    except Exception:
                        ts = ""
                    duplicates_with_ts.append({**dup, "_timestamp": ts})
                duplicates_with_ts.sort(key=lambda x: x["_timestamp"], reverse=True)
                keeper = duplicates_with_ts[0]
                to_remove = duplicates_with_ts[1:]
                for dup in to_remove:
                    try:
                        wal_log_async(
                            "consolidate_delete",
                            {"deleted_id": dup["id"], "topic": topic, "keeper_id": keeper["id"]},
                            wal_file=get_wal_path(settings.wal_dir),
                        )
                        col.delete(ids=[dup["id"]])
                        merged_count += 1
                    except Exception:
                        pass
                invalidate_query_cache()
                _invalidate_status_cache()
            return {"topic": topic, "duplicates": duplicates, "merged": merged_count if merge else None, "total_found": len(duplicates)}
        except Exception as e:
            return {"error": str(e)}
