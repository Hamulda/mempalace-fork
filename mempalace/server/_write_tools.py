"""
Write tools: add_drawer, delete_drawer, diary_write, diary_read, consolidate.
"""
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from fastmcp import Context

logger = logging.getLogger(__name__)


def _is_shared_server_mode(server) -> bool:
    """Check if shared server mode is enabled.

    Uses isinstance(bool) to distinguish actual True from MagicMock auto-created
    attributes (which are MagicMock instances, not bool). This matters in tests
    where MagicMock auto-creates attributes on access.
    """
    val = getattr(server, "_shared_server_mode", False)
    return isinstance(val, bool) and val is True


def _claim_check(server, target_id: str, session_id: str | None, mode: str | None = None) -> dict | None:
    """
    Check claim before a write operation.

    Returns None (proceed) if:
      - no ClaimsManager available (non-shared mode) — fail open
      - session_id is None — fail open, no session identity to correlate
      - no active conflict on the target
      - session_id owns the target claim — proceed

    Returns an error dict (block) only when:
      - mode=strict AND another session holds an active claim

    SHARED SERVER MODE DEFAULT: In shared server mode (HTTP transport or
    shared_server_mode=True), the effective default is strict enforcement.
    This makes 6 parallel Claude Code sessions safe by default. Callers can
    still override with claim_mode="advisory" on individual calls to get the
    old advisory behavior (warn but allow write).
    """
    if session_id is None:
        return None  # No session identity — fail open
    claims_mgr = getattr(server, "_claims_manager", None)
    if claims_mgr is None:
        return None  # Non-shared mode — no coordination available

    conflict = claims_mgr.check_conflicts("file", target_id, session_id)
    if not conflict.get("has_conflict"):
        return None  # No active conflict
    if conflict.get("is_self"):
        return None  # Self holds the claim — proceed

    # Determine effective enforcement mode:
    # - In shared server mode: strict by default, advisory opt-out only when explicitly passed
    # - Non-shared mode: advisory default (backward compatible)
    shared_mode = _is_shared_server_mode(server)
    if shared_mode:
        # Shared mode: strict unless caller explicitly said advisory
        effective_mode = "strict" if mode != "advisory" else "advisory"
    elif mode == "strict":
        effective_mode = "strict"
    else:
        effective_mode = "advisory"

    if effective_mode == "strict":
        return {
            "error": "claim_conflict",
            "owner": conflict["owner"],
            "target_id": target_id,
            "conflict_type": "active_claim",
            "suggested_action": "wait_for_handoff_or_expiry",
            "retry_after_seconds": 60,
            "hint": f"Session '{conflict['owner']}' holds an active claim on {target_id}. "
                    f"Wait ~60s for TTL expiry or request a handoff from that session.",
        }
    # advisory — warn but allow write; return warning dict so caller surfaces it
    return {
        "warning": "claim_advisory_write",
        "owner": conflict["owner"],
        "target_id": target_id,
        "message": f"Proceeding with write despite active claim by {conflict['owner']} on {target_id}.",
        "conflict_type": "active_claim",
        "suggested_action": "surface_warning_to_user",
    }


def register_write_tools(server, backend, config, settings, memory_guard):
    """
    Register all write @mcp.tool() as closures over backend/config/kg.
    Called by factory._register_tools().
    """
    from ._infrastructure import wal_log, get_wal_path, bg_executor
    from ..searcher import invalidate_query_cache
    from ..entity_detector import extract_candidates
    from ..config import sanitize_name, sanitize_content

    # Capture WriteCoordinator for intent lifecycle — fail-open if unavailable
    _wc = getattr(server, "_write_coordinator", None)

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
        # Invalidate this server instance's status cache only.
        # Uses per-server StatusCache attached to server in create_server().
        server._status_cache.invalidate()

    def _log_intent(session_id, operation, target_type, target_id, payload=None):
        """Log intent — fail-open, returns intent_id or None."""
        if _wc is None or session_id is None:
            return None
        try:
            return _wc.log_intent(session_id, operation, target_type, target_id, payload)
        except Exception as e:
            logger.warning("_log_intent failed (fail-open, intent dropped): %s", e)
            return None

    def _commit_intent(intent_id, session_id):
        """Commit intent — fail-open."""
        if _wc is None or intent_id is None or session_id is None:
            return
        try:
            _wc.commit_intent(intent_id, session_id)
        except Exception as e:
            logger.warning("_commit_intent failed (fail-open): %s", e)

    def _rollback_intent(intent_id, session_id):
        """Rollback intent — fail-open."""
        if _wc is None or intent_id is None or session_id is None:
            return
        try:
            _wc.rollback_intent(intent_id, session_id)
        except Exception as e:
            logger.warning("_rollback_intent failed (fail-open): %s", e)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_add_drawer(
        ctx: Context,
        wing: str,
        room: str,
        content: str,
        source_file: str | None = None,
        added_by: str = "mcp",
        session_id: str | None = None,
        claim_mode: str | None = None,
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

        # None = caller didn't specify; shared mode will upgrade to strict
        if claim_mode is None and _is_shared_server_mode(server):
            effective_mode = "strict"
        elif claim_mode is not None:
            effective_mode = claim_mode
        else:
            effective_mode = "advisory"
        target_id = f"{settings.palace_path}/{wing}/{room}"
        claim_err = _claim_check(server, target_id, session_id, effective_mode)
        claim_warning = None
        if claim_err:
            if "error" in claim_err:
                return claim_err
            claim_warning = claim_err.get("message")

        wal_log(
            "add_drawer",
            {"drawer_id": drawer_id, "wing": wing, "room": room, "added_by": added_by,
             "content_length": len(content), "content_preview": content[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )

        intent_id = _log_intent(session_id, "add_drawer", "drawer", drawer_id, {"wing": wing, "room": room})

        try:
            existing = col.get(ids=[drawer_id])
            if existing and existing["ids"]:
                _rollback_intent(intent_id, session_id)
                return {"success": True, "reason": "already_exists", "drawer_id": drawer_id}
        except Exception:
            pass

        if memory_guard is not None:
            try:
                if memory_guard.should_pause_writes():
                    reason = f"memory pressure: {memory_guard.pressure.value} ({memory_guard.used_ratio:.0%} used)"
                    _rollback_intent(intent_id, session_id)
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
            fts5_warning = col.upsert(
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
            _commit_intent(intent_id, session_id)
            invalidate_query_cache()
            _invalidate_status_cache()
            resp = {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room}
            if claim_warning:
                resp["claim_warning"] = claim_warning
            if fts5_warning:
                resp["_warning"] = fts5_warning
            return resp
        except Exception as e:
            _rollback_intent(intent_id, session_id)
            return {"success": False, "error": str(e), "error_code": "LANCE_WRITE_FAILED", "retryable": True}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_delete_drawer(
        ctx: Context,
        drawer_id: str,
        session_id: str | None = None,
        claim_mode: str | None = None,
    ) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        existing = col.get(ids=[drawer_id])
        if not existing["ids"]:
            return {"success": False, "error": f"Drawer not found: {drawer_id}"}
        deleted_content = existing.get("documents", [""])[0] if existing.get("documents") else ""
        deleted_meta = existing.get("metadatas", [{}])[0] if existing.get("metadatas") else {}

        if claim_mode is None and _is_shared_server_mode(server):
            effective_mode = "strict"
        elif claim_mode is not None:
            effective_mode = claim_mode
        else:
            effective_mode = "advisory"
        claim_err = _claim_check(server, drawer_id, session_id, effective_mode)
        claim_warning = None
        if claim_err:
            if "error" in claim_err:
                return claim_err
            claim_warning = claim_err.get("message")

        wal_log(
            "delete_drawer",
            {"drawer_id": drawer_id, "deleted_meta": deleted_meta, "content_preview": deleted_content[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )

        intent_id = _log_intent(session_id, "delete_drawer", "drawer", drawer_id)

        try:
            fts5_warning = col.delete(ids=[drawer_id])
            _commit_intent(intent_id, session_id)
            invalidate_query_cache()
            _invalidate_status_cache()
            resp = {"success": True, "drawer_id": drawer_id}
            if claim_warning:
                resp["claim_warning"] = claim_warning
            if fts5_warning:
                resp["_warning"] = fts5_warning
            return resp
        except Exception as e:
            _rollback_intent(intent_id, session_id)
            return {"success": False, "error": str(e), "error_code": "LANCE_WRITE_FAILED", "retryable": True}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_diary_write(
        ctx: Context,
        agent_name: str,
        entry: str,
        topic: str = "general",
        session_id: str | None = None,
        claim_mode: str | None = None,
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

        if claim_mode is None and _is_shared_server_mode(server):
            effective_mode = "strict"
        elif claim_mode is not None:
            effective_mode = claim_mode
        else:
            effective_mode = "advisory"
        target_id = f"{settings.palace_path}/{wing}/{room}"
        claim_err = _claim_check(server, target_id, session_id, effective_mode)
        claim_warning = None
        if claim_err:
            if "error" in claim_err:
                return claim_err
            claim_warning = claim_err.get("message")

        wal_log(
            "diary_write",
            {"agent_name": agent_name, "topic": topic, "entry_id": entry_id, "entry_preview": entry[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )

        intent_id = _log_intent(session_id, "diary_write", "diary_entry", entry_id, {"agent_name": agent_name, "topic": topic})

        try:
            fts5_warning = col.upsert(
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
            _commit_intent(intent_id, session_id)
            invalidate_query_cache()
            _invalidate_status_cache()
            resp = {"success": True, "entry_id": entry_id, "agent": agent_name, "topic": topic, "timestamp": now.isoformat()}
            if claim_warning:
                resp["claim_warning"] = claim_warning
            if fts5_warning:
                resp["_warning"] = fts5_warning
            return resp
        except Exception as e:
            _rollback_intent(intent_id, session_id)
            return {"success": False, "error": str(e), "error_code": "LANCE_WRITE_FAILED", "retryable": True}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_diary_read(ctx: Context, agent_name: str, last_n: int = 10, session_id: str | None = None) -> dict:
        """
        Read diary entries for an agent.

        session_id is accepted for symmetry with diary_write but is not currently
        used in the query (diary entries are per-agent, not per-session).
        Included for future session-scoped diary filtering if needed.
        """
        wing = f"wing_{agent_name.lower().replace(' ', '_')}"
        col = _get_collection()
        if not col:
            return _no_palace()
        try:
            entries = []
            try:
                # FIX: Use .search().where() + .sort() + .limit() for server-side
                # ordering and early-exit — avoids loading all entries into RAM
                # and sorting Python-side. Sort by timestamp DESC, limit to last_n*2
                # (over-fetch slightly in case some are filtered) then slice to last_n.
                over_fetch = last_n * 2
                try:
                    # LanceDB 0.4+: native sort + limit on metadata queries
                    from lancedb.query import AsyncQuery, LanceQueryBuilder
                    results = (
                        col.search()
                        .where(f"wing = '{wing}' AND room = 'diary'", prefilter=True)
                        .sort("timestamp", ascending=False)
                        .limit(over_fetch)
                        .to_pandas()
                    )
                    if not results.empty:
                        for _, row in results.iterrows():
                            try:
                                meta = json.loads(row["metadata_json"]) if row.get("metadata_json") else {}
                            except (json.JSONDecodeError, TypeError):
                                meta = {}
                            entries.append({
                                "date": meta.get("date", ""),
                                "timestamp": meta.get("timestamp", ""),
                                "topic": meta.get("topic", ""),
                                "content": str(row["document"]),
                            })
                    entries = entries[:last_n]
                except (ImportError, AttributeError):
                    # FALLBACK: LanceDB version doesn't support sort() on .search()
                    # Use heapq.nlargest to keep only the newest last_n entries in memory
                    # instead of loading all entries then sorting everything.
                    import heapq
                    _BATCH = 500
                    offset = 0
                    # Maintain a bounded heap of the newest last_n entries seen.
                    # heapq.nlargest gives us O(M log N) instead of O(M log M) full sort.
                    top_entries: list[tuple[str, dict]] = []  # (timestamp, entry_dict)
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
                            ts = meta.get("timestamp", "")
                            entry = {
                                "date": meta.get("date", ""), "timestamp": ts,
                                "topic": meta.get("topic", ""), "content": doc,
                            }
                            if ts:
                                if len(top_entries) < last_n:
                                    heapq.heappush(top_entries, (ts, entry))
                                elif ts > top_entries[0][0]:
                                    heapq.heapreplace(top_entries, (ts, entry))
                        if len(docs) < _BATCH:
                            break
                        offset += len(docs)
                    # heap is min-heap by timestamp; nlargest gives newest-first order
                    entries = [entry for _, entry in heapq.nlargest(last_n, top_entries)]
            except Exception:
                pass
            if not entries:
                return {"agent": agent_name, "entries": [], "message": "No diary entries yet."}
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
        session_id: str | None = None,
        claim_mode: str | None = None,
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

        if claim_mode is None and _is_shared_server_mode(server):
            effective_mode = "strict"
        elif claim_mode is not None:
            effective_mode = claim_mode
        else:
            effective_mode = "advisory"
        target_id = f"{settings.palace_path}/{wing}/{room}"
        claim_err = _claim_check(server, target_id, session_id, effective_mode)
        claim_warning = None
        if claim_err:
            if "error" in claim_err:
                return claim_err
            claim_warning = claim_err.get("message")

        wal_log(
            "remember_code",
            {"drawer_id": drawer_id, "wing": wing, "room": room, "added_by": added_by,
             "code_length": len(code), "description_preview": description[:200]},
            wal_file=get_wal_path(settings.wal_dir),
        )

        intent_id = _log_intent(session_id, "remember_code", "drawer", drawer_id, {"wing": wing, "room": room})

        try:
            existing = col.get(ids=[drawer_id])
            if existing and existing["ids"]:
                _rollback_intent(intent_id, session_id)
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
            fts5_warning = col.upsert(
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
            _commit_intent(intent_id, session_id)
            invalidate_query_cache()
            _invalidate_status_cache()
            resp = {
                "success": True, "drawer_id": drawer_id, "wing": wing, "room": room,
                "code_truncated": was_truncated, "original_length": len(code),
                "stored_length": len(code_stored), "language": language,
            }
            if claim_warning:
                resp["claim_warning"] = claim_warning
            if fts5_warning:
                resp["_warning"] = fts5_warning
            return resp
        except Exception as e:
            _rollback_intent(intent_id, session_id)
            return {"success": False, "error": str(e), "error_code": "LANCE_WRITE_FAILED", "retryable": True}

    @server.tool(timeout=settings.timeout_embed)
    def mempalace_consolidate(
        ctx: Context,
        topic: str,
        merge: bool = False,
        threshold: float = 0.85,
        session_id: str | None = None,
        claim_mode: str | None = None,
    ) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        consolidate_intent_id = None
        claim_warning = None
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
                # Enforce claim before any write (delete) in merge path.
                if claim_mode is None and _is_shared_server_mode(server):
                    effective_mode = "strict"
                elif claim_mode is not None:
                    effective_mode = claim_mode
                else:
                    effective_mode = "advisory"
                consolidate_target = f"{settings.palace_path}/_consolidate/{topic}"
                claim_err = _claim_check(server, consolidate_target, session_id, effective_mode)
                if claim_err:
                    if "error" in claim_err:
                        return claim_err
                    claim_warning = claim_err.get("message")

                consolidate_intent_id = _log_intent(
                    session_id, "consolidate", "consolidate", consolidate_target,
                    {"topic": topic, "duplicates_count": len(duplicates)}
                )

                # Fetch all timestamps in one batch call instead of N individual col.get()
                all_ids = [dup["id"] for dup in duplicates]
                ts_map: dict[str, str] = {}
                try:
                    raw = col.get(ids=all_ids, include=["metadatas"])
                    for i, mid in enumerate(raw["ids"][0] if raw["ids"] else []):
                        meta = raw["metadatas"][0][i] if raw["metadatas"] and i < len(raw["metadatas"][0]) else {}
                        ts_map[mid] = meta.get("timestamp", "") if meta else ""
                except Exception:
                    ts_map = {}
                duplicates_with_ts = [{**dup, "_timestamp": ts_map.get(dup["id"], "")} for dup in duplicates]
                duplicates_with_ts.sort(key=lambda x: x["_timestamp"], reverse=True)
                keeper = duplicates_with_ts[0]
                to_remove = duplicates_with_ts[1:]
                consolidate_failed = False
                fts5_warning = None
                if to_remove:
                    try:
                        wal_log(
                            "consolidate_delete",
                            {"deleted_ids": [d["id"] for d in to_remove], "topic": topic, "keeper_id": keeper["id"]},
                            wal_file=get_wal_path(settings.wal_dir),
                        )
                        fts5_warning = col.delete(ids=[d["id"] for d in to_remove])
                        merged_count = len(to_remove)
                    except Exception:
                        consolidate_failed = True
                if consolidate_failed:
                    _rollback_intent(consolidate_intent_id, session_id)
                else:
                    _commit_intent(consolidate_intent_id, session_id)
                invalidate_query_cache()
                _invalidate_status_cache()
            resp = {"topic": topic, "duplicates": duplicates, "merged": merged_count if merge else None, "total_found": len(duplicates)}
            if claim_warning:
                resp["claim_warning"] = claim_warning
            if merge and fts5_warning:
                resp["_warning"] = fts5_warning
            return resp
        except Exception as e:
            _rollback_intent(consolidate_intent_id, session_id)
            return {"error": str(e), "error_code": "LANCE_WRITE_FAILED", "retryable": True}