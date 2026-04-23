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
        """
        Query facts for an entity from the knowledge graph.

        Args:
            entity: The subject or object entity to query facts about.
            as_of: Optional ISO timestamp for historical point-in-time query.
            direction: Which facts to return — "both" (default), "incoming",
                or "outgoing". Incoming = facts where entity is the object;
                outgoing = facts where entity is the subject.
            active_only: If True, only return facts that have not been
                superseded or ended (default False).

        Returns:
            dict with entity, as_of, facts list, and count.
        """
        results = kg.query_entity(entity, as_of=as_of, direction=direction, active_only=active_only)
        return {"entity": entity, "as_of": as_of, "facts": results, "count": len(results)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_kg_add(
        ctx: Context,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: str | None = None,
        source_closet: str | None = None,
    ) -> dict:
        """
        Add a fact (subject-predicate-object triple) to the knowledge graph.

        Args:
            subject: The entity that is the subject of the fact.
            predicate: The relationship or property (e.g., "is_a", "authored_by").
            obj: The entity that is the object of the fact.
            valid_from: Optional ISO timestamp for when this fact becomes valid.
            source_closet: Optional source identifier for provenance tracking.

        Returns:
            dict with success=True, triple_id, and a human-readable fact string.
        """
        try:
            subject = sanitize_name(subject, "subject")
            predicate = sanitize_name(predicate, "predicate")
            obj = sanitize_name(obj, "object")
        except ValueError as e:
            return {"success": False, "error": str(e)}
        wal_log_async(
            "kg_add",
            {"subject": subject, "predicate": predicate, "object": obj,
             "valid_from": valid_from, "source_closet": source_closet},
            wal_file=get_wal_path(settings.wal_dir),
        )
        triple_id = kg.add_triple(subject, predicate, obj, valid_from=valid_from, source_closet=source_closet)
        return {"success": True, "triple_id": triple_id, "fact": f"{subject} → {predicate} → {obj}"}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_kg_invalidate(
        ctx: Context,
        subject: str,
        predicate: str,
        obj: str,
        ended: str | None = None,
    ) -> dict:
        """
        Invalidate (end) all versions of a specific fact.

        Unlike supersede which replaces one value with another, invalidate
        simply marks the fact as ended at a given timestamp or now.

        Args:
            subject: The subject entity of the fact to invalidate.
            predicate: The predicate of the fact to invalidate.
            obj: The object value to invalidate (matches all versions).
            ended: Optional ISO timestamp for when the fact ended
                (default: "today" / current time).

        Returns:
            dict with success=True, fact string, and ended timestamp.
        """
        wal_log_async(
            "kg_invalidate",
            {"subject": subject, "predicate": predicate, "object": obj, "ended": ended},
            wal_file=get_wal_path(settings.wal_dir),
        )
        kg.invalidate(subject, predicate, obj, ended=ended)
        return {"success": True, "fact": f"{subject} → {predicate} → {obj}", "ended": ended or "today"}

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
        """
        Replace one value of a fact with a new value, preserving history.

        Supersedes all existing triples matching (subject, predicate, old_value)
        and creates a new triple with new_value. Both old and new triples
        are retained in the knowledge graph with validity time ranges.

        Args:
            subject: The subject entity whose fact is being updated.
            predicate: The predicate of the fact being updated.
            old_value: The object value to supersede.
            new_value: The new object value to record.
            agent_id: Optional agent identifier for provenance (default "unknown").
            source_closet: Optional source identifier for provenance.

        Returns:
            dict with success=True and the supersede result (old triple retired,
            new triple created with validity timestamps).
        """
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
        """
        Get a timeline of all changes to the knowledge graph.

        When entity is None, returns all recent changes across all entities.
        When entity is specified, returns only changes affecting that entity.

        Args:
            entity: Optional entity name to filter the timeline to.
                If None, returns changes for all entities.

        Returns:
            dict with entity (or "all"), timeline list, and count.
            Each timeline entry contains the change type and affected triple info.
        """
        results = kg.timeline(entity)
        return {"entity": entity or "all", "timeline": results, "count": len(results)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_stats(ctx: Context) -> dict:
        """
        Get statistics about the knowledge graph.

        Returns:
            dict with KG statistics: total triples, entity count, predicate
            count, and other metrics about the knowledge graph state.
        """
        return kg.stats()

    @server.tool(timeout=settings.timeout_read)
    def mempalace_kg_history(ctx: Context, subject: str, predicate: str) -> dict:
        """
        Get the full version history for facts matching a subject and predicate.

        Returns all versions of every triple with the given subject and predicate,
        including superseded (historical) versions, ordered chronologically.

        Args:
            subject: The subject entity to look up history for.
            predicate: The predicate to look up history for.

        Returns:
            dict with subject, predicate, history list (all versions),
            versions count, and current (the most recent active triple or None).
        """
        history = kg.get_triple_history(subject, predicate)
        return {
            "subject": subject, "predicate": predicate, "history": history,
            "versions": len(history),
            "current": next((h for h in history if h["current"]), None),
        }
