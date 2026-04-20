"""
Session coordination tools: claims, handoffs, decisions, wakeup.
"""
import os
from fastmcp import Context


# ── Session ID auto-detection ────────────────────────────────────────────────

def _get_session_id_from_ctx(ctx: Context) -> str | None:
    """
    Extract session_id from FastMCP Context if available.

    FastMCP embeds session_id in the request context. This helper lets tools
    auto-detect it instead of requiring explicit passing — reduces boilerplate
    for 6 parallel Claude Code sessions.

    Returns None if session_id cannot be determined (non-HTTP transport or
    context not available). Tools that require session_id will raise an error
    in that case, maintaining explicit semantics.
    """
    try:
        # FastMCP Context exposes request context — check for session_id.
        # Pattern verified against SessionTrackingMiddleware (middleware.py:269).
        request_ctx = getattr(ctx, "request_context", None)
        if request_ctx is not None:
            session_id = getattr(request_ctx, "session_id", None)
            if session_id:
                return session_id
        # Fallback: try fastmcp_context attribute directly (used by middleware).
        fastmcp_ctx = getattr(ctx, "fastmcp_context", None)
        if fastmcp_ctx is not None:
            session_id = getattr(fastmcp_ctx, "session_id", None)
            if session_id:
                return session_id
    except Exception:
        pass
    # Last resort: MEMPALACE_SESSION_ID env var (set by Claude Code harness).
    return os.environ.get("MEMPALACE_SESSION_ID")


def _require_session_id(ctx: Context, explicit: str | None, action: str) -> str:
    """
    Resolve session_id for a tool call.

    Priority:
    1. Explicitly passed session_id (from tool caller)
    2. Auto-detected from FastMCP context (session from Claude Code harness)
    3. Raise ValueError — caller must provide session_id for this action.

    This ensures session identity is always explicit in logs and coordination,
    while eliminating the common case where model just passes ctx through.
    """
    if explicit:
        return explicit
    auto = _get_session_id_from_ctx(ctx)
    if auto:
        return auto
    raise ValueError(
        f"{action} requires session_id. "
        "Pass it explicitly or ensure MEMPALACE_SESSION_ID is set in environment."
    )


def _optional_session_id(ctx: Context, explicit: str | None) -> str | None:
    """
    Resolve session_id optionally — returns None if neither explicit nor auto-detected.

    Use for tools that benefit from session context when available but don't
    require it (e.g., diary_read, list_claims).
    """
    if explicit:
        return explicit
    return _get_session_id_from_ctx(ctx)


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
        session_id: str | None = None,
        ttl_seconds: int = 600,
        note: str | None = None,
    ) -> dict:
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _require_session_id(ctx, session_id, "claim_path")
        payload = {"note": note, "path": path} if note else {"path": path}
        return mgr.claim("file", path, resolved, ttl_seconds=ttl_seconds, payload=payload)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_release_claim(ctx: Context, path: str, session_id: str | None = None) -> dict:
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _require_session_id(ctx, session_id, "release_claim")
        return mgr.release_claim("file", path, resolved)

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
    def mempalace_conflict_check(ctx: Context, path: str, session_id: str | None = None) -> dict:
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _require_session_id(ctx, session_id, "conflict_check")
        return mgr.check_conflicts("file", path, resolved)

    # ── Handoffs ────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_push_handoff(
        ctx: Context,
        summary: str,
        touched_paths: list[str] | None = None,
        blockers: list[str] | None = None,
        next_steps: list[str] | None = None,
        confidence: int = 3,
        priority: str = "normal",
        from_session_id: str | None = None,
        to_session_id: str | None = None,
    ) -> dict:
        """
        Push a handoff to another session (or broadcast if to_session_id=None).

        Improved UX — most fields now have sensible defaults:
        - from_session_id: auto-detected from context if not provided
        - touched_paths: defaults to [] (can be added later via diary or symbol tools)
        - blockers: defaults to [] (optional — omit if nothing blocking)
        - next_steps: defaults to [] (can be captured later via capture_decision)
        - confidence: defaults to 3 (model doesn't need to think about it unless high/low)

        Required: summary (what was done / what the handoff is about)
        """
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved_from = _require_session_id(ctx, from_session_id, "push_handoff")
        return mgr.push_handoff(
            from_session_id=resolved_from, summary=summary,
            touched_paths=touched_paths or [],
            blockers=blockers or [],
            next_steps=next_steps or [],
            confidence=confidence,
            priority=priority, to_session_id=to_session_id,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_pull_handoffs(ctx: Context, session_id: str | None = None, status: str | None = None) -> dict:
        """
        Pull pending handoffs for this session.

        session_id is optional — if not provided, pulls all broadcast handoffs
        (to_session_id=None). This is useful when waking up and wanting to
        see what's available for anyone to pick up.
        """
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _optional_session_id(ctx, session_id)
        handoffs = mgr.pull_handoffs(session_id=resolved, status=status)
        return {"handoffs": handoffs, "count": len(handoffs)}

    @server.tool(timeout=settings.timeout_write)
    def mempalace_accept_handoff(ctx: Context, handoff_id: str, session_id: str | None = None) -> dict:
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _require_session_id(ctx, session_id, "accept_handoff")
        return mgr.accept_handoff(handoff_id, resolved)

    @server.tool(timeout=settings.timeout_write)
    def mempalace_complete_handoff(ctx: Context, handoff_id: str, session_id: str | None = None) -> dict:
        mgr = _get_handoff_manager()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _require_session_id(ctx, session_id, "complete_handoff")
        return mgr.complete_handoff(handoff_id, resolved)

    # ── Wakeup ──────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_wakeup_context(
        ctx: Context,
        session_id: str | None = None,
        project_root: str | None = None,
    ) -> dict:
        """
        Build a compact wake-up context bundle for session resume or takeover.

        Improved UX — session_id and project_root are now auto-detected:
        - session_id: from FastMCP context (Claude Code harness) or MEMPALACE_SESSION_ID env var
        - project_root: from PROJECT_ROOT env var (defaults to "" if not set)

        Call with no arguments for full auto-detection:
            mempalace_wakeup_context()
        """
        try:
            resolved_sid = _require_session_id(ctx, session_id, "wakeup_context")
            # project_root auto-detected inside build_wakeup_context from env/PROJECT_ROOT
            result = build_wakeup_context(
                session_id=resolved_sid,
                project_root=project_root,  # None → build_wakeup_context uses PROJECT_ROOT env
                palace_path=config.palace_path,
            )
            return result
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    # ── Decisions ───────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_capture_decision(
        ctx: Context,
        decision: str,
        rationale: str,
        alternatives: list[str] | None = None,
        category: str = "general",
        confidence: int = 3,
        session_id: str | None = None,
    ) -> dict:
        """
        Capture a significant decision made during this session.

        Improved UX — session_id auto-detected, confidence defaults to 3,
        alternatives defaults to [], category defaults to "general".

        Required: decision (what was decided), rationale (why).
        """
        mgr = _get_decision_tracker()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _require_session_id(ctx, session_id, "capture_decision")
        return mgr.capture_decision(
            session_id=resolved, decision_text=decision, rationale=rationale,
            alternatives=alternatives or [], category=category, confidence=confidence,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_list_decisions(
        ctx: Context,
        session_id: str | None = None,
        category: str | None = None,
    ) -> dict:
        """
        List decisions from this session (or all sessions if session_id=None).

        session_id is optional — auto-detected from context if not provided.
        """
        mgr = _get_decision_tracker()
        if mgr is None:
            return {"error": "session coordination not available"}
        resolved = _optional_session_id(ctx, session_id)
        decisions = mgr.list_decisions(session_id=resolved, category=category)
        return {"decisions": decisions, "count": len(decisions)}
