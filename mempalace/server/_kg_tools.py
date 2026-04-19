"""
Knowledge-graph tools: query, add, invalidate, supersede, timeline, history, stats.
"""
import os
from fastmcp import Context


def register_kg_tools(server, backend, config, settings):
    """
    Register all KG @mcp.tool() as closures over kg instance.
    Called by factory._register_tools().
    """
    from ._infrastructure import wal_log_async, get_wal_path
    from ..knowledge_graph import KnowledgeGraph
    from ..config import sanitize_name

    kg = KnowledgeGraph(db_path=os.path.join(settings.db_path, "knowledge_graph.sqlite3"))

    def _no_palace():
        return {"error": "No palace found", "hint": "Run: mempalace init <dir> && mempalace mine <dir>"}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_query(
        ctx: Context,
        entity: str,
        as_of: str | None = None,
        direction: str = "both",
        active_only: bool = False,
    ) -> dict:
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
        try:
            subject = sanitize_name(subject, "subject")
            predicate = sanitize_name(predicate, "predicate")
            object = sanitize_name(object, "object")
        except ValueError as e:
            return {"success": False, "error": str(e)}
        wal_log_async(
            "kg_add",
            {"subject": subject, "predicate": predicate, "object": object,
             "valid_from": valid_from, "source_closet": source_closet},
            wal_file=get_wal_path(settings.wal_dir),
        )
        triple_id = kg.add_triple(subject, predicate, object, valid_from=valid_from, source_closet=source_closet)
        return {"success": True, "triple_id": triple_id, "fact": f"{subject} → {predicate} → {object}"}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_kg_invalidate(
        ctx: Context,
        subject: str,
        predicate: str,
        object: str,
        ended: str | None = None,
    ) -> dict:
        wal_log_async(
            "kg_invalidate",
            {"subject": subject, "predicate": predicate, "object": object, "ended": ended},
            wal_file=get_wal_path(settings.wal_dir),
        )
        kg.invalidate(subject, predicate, object, ended=ended)
        return {"success": True, "fact": f"{subject} → {predicate} → {object}", "ended": ended or "today"}

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
        try:
            subject = sanitize_name(subject, "subject")
            predicate = sanitize_name(predicate, "predicate")
        except ValueError as e:
            return {"success": False, "error": str(e)}
        wal_log_async(
            "kg_supersede",
            {"subject": subject, "predicate": predicate, "old_value": old_value,
             "new_value": new_value, "agent_id": agent_id},
            wal_file=get_wal_path(settings.wal_dir),
        )
        result = kg.supersede_triple(subject, predicate, old_value, new_value,
                                     agent_id=agent_id, source_closet=source_closet)
        return {"success": True, **result}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_timeline(ctx: Context, entity: str | None = None) -> dict:
        results = kg.timeline(entity)
        return {"entity": entity or "all", "timeline": results, "count": len(results)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_stats(ctx: Context) -> dict:
        return kg.stats()

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_history(ctx: Context, subject: str, predicate: str) -> dict:
        history = kg.get_triple_history(subject, predicate)
        return {
            "subject": subject, "predicate": predicate, "history": history,
            "versions": len(history),
            "current": next((h for h in history if h["current"]), None),
        }
