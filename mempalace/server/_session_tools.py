"""
Session coordination tools: claims, handoffs, decisions, wakeup.
"""
from fastmcp import Context


def register_session_tools(server, backend, config, settings):
    """
    Register all session-coordination @mcp.tool() as closures.
    Called by factory._register_tools().
    """
    from ..wakeup_context import build_wakeup_context

    def _get_claims_manager():
        return getattr(server, "_claims_manager", None)

    def _get_handoff_manager():
        return getattr(server, "_handoff_manager", None)

    def _get_decision_tracker():
        return getattr(server, "_decision_tracker", None)

    # ── Claims ───────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_claim_path(
        ctx: Context,
        path: str,
        session_id: str,
        ttl_seconds: int = 600,
        note: str | None = None,
    ) -> dict:
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        payload = {"note": note, "path": path} if note else {"path": path}
        return mgr.claim("file", path, session_id, ttl_seconds=ttl_seconds, payload=payload)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_release_claim(ctx: Context, path: str, session_id: str) -> dict:
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        return mgr.release_claim("file", path, session_id)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_claims(ctx: Context, session_id: str | None = None) -> dict:
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        if session_id:
            claims = mgr.get_session_claims(session_id)
        else:
            claims = mgr.list_active_claims()
        return {"claims": claims, "count": len(claims)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_conflict_check(ctx: Context, path: str, session_id: str) -> dict:
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        return mgr.check_conflicts("file", path, session_id)

    # ── Handoffs ────────────────────────────────────────────────────────────

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
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        return mgr.push_handoff(
            from_session_id=from_session_id, summary=summary, touched_paths=touched_paths,
            blockers=blockers, next_steps=next_steps, confidence=confidence,
            priority=priority, to_session_id=to_session_id,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_pull_handoffs(ctx: Context, session_id: str, status: str | None = None) -> dict:
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        handoffs = mgr.pull_handoffs(session_id=session_id, status=status)
        return {"handoffs": handoffs, "count": len(handoffs)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_accept_handoff(ctx: Context, handoff_id: str, session_id: str) -> dict:
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        return mgr.accept_handoff(handoff_id, session_id)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_complete_handoff(ctx: Context, handoff_id: str, session_id: str) -> dict:
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        return mgr.complete_handoff(handoff_id, session_id)

    # ── Wakeup ──────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_wakeup_context(ctx: Context, session_id: str, project_root: str | None = None) -> dict:
        try:
            result = build_wakeup_context(
                session_id=session_id, project_root=project_root, palace_path=config.palace_path,
            )
            return result
        except Exception as e:
            return {"error": str(e)}

    # ── Decisions ───────────────────────────────────────────────────────────

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
        mgr = _get_decision_tracker()
        if mgr is None:
            return {"error": "session coordination not available"}
        return mgr.capture_decision(
            session_id=session_id, decision_text=decision, rationale=rationale,
            alternatives=alternatives, category=category, confidence=confidence,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_decisions(ctx: Context, session_id: str | None = None, category: str | None = None) -> dict:
        mgr = _get_decision_tracker()
        if mgr is None:
            return {"error": "session coordination not available"}
        decisions = mgr.list_decisions(session_id=session_id, category=category)
        return {"decisions": decisions, "count": len(decisions)}
