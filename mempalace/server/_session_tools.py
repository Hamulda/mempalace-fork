"""
Session coordination tools: claims, handoffs, decisions, wakeup.
"""
import os
from fastmcp import Context


# ── Project root resolution (imported from canonical source) ───────────────────
from ._project_root import _find_git_root, _resolve_project_root

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

    def _get_symbol_index():
        try:
            from ..symbol_index import SymbolIndex
            return SymbolIndex.get(settings.palace_path)
        except Exception:
            return None

    def _hotspot_check(path: str, project_root: str | None) -> dict:
        """Return hotspot metadata for a path from recent git changes."""
        if not project_root:
            project_root = _find_git_root(path)
        if not project_root:
            return {}
        try:
            from ..recent_changes import get_recent_changes
            changes = get_recent_changes(project_root, n=20)
            file_changes = [
                c for c in changes
                if c.get("file_path") == path or c.get("abs_path") == path
            ]
            if not file_changes:
                return {}
            fc = file_changes[0]
            return {
                "hotspot": fc.get("change_count", 0) >= 3,
                "change_count": fc.get("change_count", 0),
                "last_modified": fc.get("last_modified"),
            }
        except Exception:
            return {}

    # ── Claims ───────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_claim_path(
        ctx: Context,
        path: str,
        session_id: str | None = None,
        ttl_seconds: int = 600,
        note: str | None = None,
    ) -> dict:
        """
        [Tier 2 — Escape Hatch] Claim a file path with TTL for exclusive editing.

        Use this ONLY when:
          - You need to refresh TTL on a claim you already hold (no conflict check needed)
          - A workflow tool returned an error and you need fine-grained control

        For normal editing: use mempalace_begin_work instead.
        """
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

    # ── File Status ─────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_file_status(
        ctx: Context,
        path: str,
        session_id: str | None = None,
    ) -> dict:
        """
        [Tier 1 — Primary Workflow] Get a compact coordination snapshot for a single file.

        Returns:
          - claim: current claim holder, expires_at, or null
          - hotspot: change count from git history, last_modified
          - symbols: count of top-level definitions (from symbol index)
          - next_actions: recommended workflow based on state

        Use this before claiming or editing to understand the full coordination picture.
        """
        mgr = _get_claims_manager()
        si = _get_symbol_index()
        resolved = _optional_session_id(ctx, session_id)
        project_root = _find_git_root(path)

        # Claim state
        claim = None
        conflict = None
        if mgr:
            conflict = mgr.check_conflicts("file", path, resolved or "")
            active = mgr.get_claim("file", path)
            if active:
                claim = {
                    "owner": active["session_id"],
                    "expires_at": active["expires_at"],
                    "is_self": conflict.get("is_self", False) if conflict else False,
                }
            elif conflict and conflict.get("has_conflict"):
                claim = {
                    "owner": conflict["owner"],
                    "expires_at": conflict["expires_at"],
                    "is_self": False,
                }

        # Hot spot check
        hotspot = _hotspot_check(path, project_root)

        # Symbol count
        symbol_count = 0
        symbols_preview = []
        if si:
            try:
                sym_data = si.get_file_symbols(path)
                symbol_count = len(sym_data.get("symbols", []))
                symbols_preview = [
                    {"name": s["name"], "type": s.get("type", "?")}
                    for s in sym_data.get("symbols", [])[:5]
                ]
            except Exception:
                pass

        # Build next_actions and workflow_state based on claim state
        next_actions = []
        if claim and claim.get("is_self"):
            next_tool = "mempalace_prepare_edit"
            next_actions.append({
                "action": "mempalace_prepare_edit",
                "reason": "You hold the claim — get symbol context before editing",
                "priority": "high",
                "skill": "prepare-edit",
            })
            if hotspot.get("hotspot"):
                next_actions.append({
                    "action": "mempalace_conflict_check",
                    "reason": f"Hot file changed {hotspot.get('change_count', 0)}x — verify no concurrent edit before editing",
                    "priority": "high",
                })
        elif claim and not claim.get("is_self"):
            next_tool = "mempalace_push_handoff"
            next_actions.append({
                "action": "mempalace_push_handoff",
                "reason": f"File claimed by '{claim['owner']}' until {claim['expires_at']} — negotiate or wait",
                "priority": "high",
            })
            next_actions.append({
                "action": "mempalace_pull_handoffs",
                "reason": "Check if owner has broadcast a handoff for this file",
                "priority": "medium",
            })
        else:
            next_tool = "mempalace_begin_work"
            next_actions.append({
                "action": "mempalace_begin_work",
                "reason": "File is unclaimed — begin your edit session",
                "priority": "high",
                "skill": "begin-work",
            })
            if hotspot.get("hotspot"):
                next_actions.append({
                    "action": "mempalace_conflict_check",
                    "reason": f"Hot file changed {hotspot.get('change_count', 0)}x recently — verify no concurrent edit",
                    "priority": "high",
                })

        return {
            "path": path,
            "session_id": resolved,
            "claim": claim,
            "hotspot": hotspot if hotspot else None,
            "symbol_count": symbol_count,
            "symbols_preview": symbols_preview,
            "next_actions": next_actions,
            "workflow_state": {
                "current_phase": "orienting",
                "next_phase": None,
                "next_tool": next_tool,
                "conflict_status": "hotspot" if hotspot.get("hotspot") else "none",
                "handoff_pending": False,
            },
        }

    # ── Workspace Claims ─────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_workspace_claims(
        ctx: Context,
        workspace: str,
        session_id: str | None = None,
    ) -> dict:
        """
        Get all active claims within a workspace prefix (e.g. /src/auth/).

        Returns:
          - workspace: the prefix queried
          - claims: list of {path, owner, expires_at, note}
          - conflicts: any claims that conflict with session_id's intent
          - hotspots: files in workspace with >=3 recent changes

        Use this to understand what's active in a whole module/directory before
        starting work that might touch multiple files.
        """
        mgr = _get_claims_manager()
        if mgr is None:
            return {"error": "session coordination not available"}

        resolved = _optional_session_id(ctx, session_id)
        project_root = _resolve_project_root(None, settings.palace_path, workspace)

        all_claims = mgr.list_active_claims()

        # Filter to workspace prefix
        workspace_claims = [
            c for c in all_claims
            if c["target_id"].startswith(workspace)
        ]

        # Parse into compact form
        claims_list = [
            {
                "path": c["target_id"],
                "owner": c["session_id"],
                "expires_at": c["expires_at"],
                "note": c.get("payload", {}).get("note", ""),
            }
            for c in workspace_claims
        ]

        # Check conflicts for resolved session
        conflicts = []
        if resolved:
            for c in workspace_claims:
                if c["session_id"] != resolved:
                    conflicts.append({
                        "path": c["target_id"],
                        "owner": c["session_id"],
                        "expires_at": c["expires_at"],
                    })

        # Hot spots in workspace
        hotspots = []
        if project_root:
            try:
                from ..recent_changes import get_recent_changes
                changes = get_recent_changes(project_root, n=30)
                for c in changes:
                    fp = c.get("file_path") or c.get("abs_path", "")
                    if fp.startswith(workspace) and c.get("change_count", 0) >= 3:
                        hotspots.append({
                            "path": fp,
                            "change_count": c.get("change_count"),
                            "last_modified": c.get("last_modified"),
                        })
            except Exception:
                pass

        next_actions = []
        if conflicts:
            next_actions.append({
                "action": "mempalace_pull_handoffs",
                "reason": f"{len(conflicts)} conflicting claims in workspace — check for broadcast handoffs",
                "priority": "high",
            })
        elif claims_list:
            next_actions.append({
                "action": "mempalace_file_status",
                "reason": "Workspace has active claims — check individual file status",
                "priority": "medium",
                "skill": "file-status",
            })
        else:
            next_actions.append({
                "action": "mempalace_begin_work",
                "reason": "Workspace is clear — no active claims",
                "priority": "high",
                "skill": "begin-work",
            })

        return {
            "workspace": workspace,
            "claims": claims_list,
            "claims_count": len(claims_list),
            "conflicts": conflicts,
            "conflicts_count": len(conflicts),
            "hotspots": hotspots,
            "next_actions": next_actions,
        }

    # ── Edit Guidance ───────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_edit_guidance(
        ctx: Context,
        workflow_result: dict,
    ) -> dict:
        """
        Extract plain-language edit guidance from a workflow_result.

        Takes any workflow tool result (begin_work, prepare_edit, finish_work,
        publish_handoff, takeover_work) and returns:
          - file_to_edit: the primary target file
          - guidance: human-readable next step in imperative mood
          - skill_ref: which skill doc to read for context
          - edit_scope: what kind of edit (quick-fix, refactor, multi-file)
          - warnings: any concerns to heed before editing

        Use this to convert structured workflow results into clear action items.
        """
        ok = workflow_result.get("ok", False)
        action = workflow_result.get("action", "")
        phase = workflow_result.get("phase", "")
        failure_mode = workflow_result.get("failure_mode")
        details = workflow_result.get("details", {})
        context_snippets = workflow_result.get("context_snippets", {})
        next_actions = workflow_result.get("next_actions", [])

        guidance = ""
        skill_ref = None
        edit_scope = "single-file"
        warnings = []
        file_to_edit = workflow_result.get("path")

        if not ok and failure_mode:
            if failure_mode == "claim_conflict":
                owner = details.get("owner", "another session")
                expires = details.get("expires_at", "unknown")
                guidance = (
                    f"STOP — {owner} holds the claim until {expires}. "
                    f"Wait for expiry, push a handoff requesting release, or work on a different file."
                )
                skill_ref = "begin-work"
                warnings.append(f"Claim conflict with {owner}")
            elif failure_mode == "no_coordination":
                guidance = "Session coordination unavailable — run with shared_server_mode=True"
                warnings.append("Coordinator not available")
            else:
                guidance = f"Workflow failed: {failure_mode}. Hint: {workflow_result.get('hint', 'retry')}"
        elif action == "begin_work" and ok:
            path = workflow_result.get("path")
            was_conflict = workflow_result.get("was_conflict", False)
            if was_conflict:
                guidance = f"You refreshed an existing claim on {path}. Review symbols, then make your edit."
            else:
                guidance = f"Claim acquired on {path}. Read the prepare-edit skill, then call mempalace_prepare_edit."
            skill_ref = "prepare-edit"
            edit_scope = "single-file"
            if next_actions:
                guidance += f" Next: {next_actions[0].get('action')}"
        elif action == "prepare_edit" and ok:
            hotspot = workflow_result.get("hotspot", False)
            symbols_count = workflow_result.get("symbols_count", 0)
            path = workflow_result.get("path")
            if hotspot:
                guidance = f"{path} is a HOT SPOT ({symbols_count} symbols) — check for concurrent edits before writing."
                warnings.append("Hot spot — high change frequency")
                skill_ref = "before-edit"
            else:
                guidance = f"{path} has {symbols_count} symbols. Safe to edit. Make your changes."
            edit_scope = "single-file"
        elif action == "finish_work" and ok:
            diary_id = workflow_result.get("diary_id")
            if diary_id:
                guidance = f"Claim released. Diary ready: {diary_id}. Write it with mempalace_diary_write."
            else:
                guidance = f"Claim released on {workflow_result.get('path')}. Edit complete."
            skill_ref = "finish-work"
        elif action == "publish_handoff" and ok:
            touched = workflow_result.get("touched_paths", [])
            handoff_id = workflow_result.get("handoff_id")
            guidance = f"Handoff {handoff_id} published for {len(touched)} file(s). Claims released. Write a diary entry."
            skill_ref = "handoff"
            edit_scope = "multi-file" if len(touched) > 1 else "single-file"
        elif action == "takeover_work" and ok:
            claimed = workflow_result.get("claimed_paths", [])
            all_acquired = workflow_result.get("all_claims_acquired", False)
            paths_str = ", ".join(p.get("path", "?") for p in claimed[:3])
            if all_acquired:
                guidance = f"Takeover complete. You now own: {paths_str}. Call mempalace_prepare_edit on each."
            else:
                guidance = f"Partial takeover: {paths_str}. Some paths blocked — check conflicts."
                warnings.append("Partial claim — some paths unavailable")
            skill_ref = "takeover"
            edit_scope = "multi-file" if len(claimed) > 1 else "single-file"
        else:
            guidance = f"Workflow '{action}' returned phase='{phase}'. Review next_actions for guidance."
            if next_actions:
                guidance += f" Start with: {next_actions[0].get('action')}"

        return {
            "ok": ok,
            "action": action,
            "file_to_edit": file_to_edit,
            "guidance": guidance,
            "skill_ref": skill_ref,
            "edit_scope": edit_scope,
            "warnings": warnings,
            "next_actions": next_actions,
        }

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

        Improved UX — session_id is auto-detected from FastMCP context or
        MEMPALACE_SESSION_ID env var. project_root is auto-detected via
        git-root derivation from palace_path (no PROJECT_ROOT env dependency).

        Call with no arguments for full auto-detection:
            mempalace_wakeup_context()
        """
        try:
            resolved_sid = _require_session_id(ctx, session_id, "wakeup_context")
            # project_root auto-detected inside build_wakeup_context via git-root derivation
            result = build_wakeup_context(
                session_id=resolved_sid,
                project_root=project_root,
                palace_path=config.palace_path,
            )
            return result
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    # ── Startup ──────────────────────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_startup_context(
        ctx: Context,
        project_path: str | None = None,
        session_id: str | None = None,
        limit: int = 8,
    ) -> dict:
        """
        Build compact startup context for Claude Code session start.

        Provides a single compact context pack with server health, palace
        overview, active claims/handoffs, and M1 bounded defaults — so Claude
        knows the project state before responding.

        Inputs:
            project_path: optional project root to scope claims (auto-derived if None)
            session_id: auto-detected from FastMCP context or MEMPALACE_SESSION_ID
            limit: max pending handoffs to return (default 8)

        Returns:
            - server_health: HTTP /health probe result
            - palace_path: palace data directory
            - backend: storage backend ('lance')
            - python_version: sys.version_info string
            - embedding_provider: embed daemon provider from probe
            - embedding_meta: model_id, embed_batch_size if available
            - active_sessions: count from session registry
            - current_claims: claims scoped to project_path
            - pending_handoffs: handoffs for this session
            - recommended_first_actions: startup workflow steps
            - project_path_reminder: resolved project_path
            - m1_defaults: bounded defaults for M1/8GB runs
        """
        from ..wakeup_context import build_startup_context
        try:
            resolved_sid = _optional_session_id(ctx, session_id)
            if not resolved_sid:
                resolved_sid = "no-session"
            result = build_startup_context(
                session_id=resolved_sid,
                project_path=project_path,
                palace_path=config.palace_path,
                limit=limit,
            )
            return result
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
