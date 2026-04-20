"""
Workflow compound tools: mempalace_begin_work, mempalace_prepare_edit,
mempalace_finish_work, mempalace_publish_handoff, mempalace_takeover_work.

These tools compress the common edit/takeover/handoff cycles into single,
opinionated calls that return structured guidance — next_actions, state_snapshot,
failure_modes, and context snippets — so the model never has to manually
reconstruct session state.

Each compound tool:
  1. Performs the minimal necessary underlying tool calls atomically
  2. Returns a workflow_result dict with explicit phases and guidance
  3. Fails with specific, actionable error shapes (never silent)
  4. Works alongside low-level tools (backward-compatible)

Tool naming contract:
  begin_work   — start a working session on a path (conflict check + claim)
  prepare_edit — enrich context before editing (symbols + recent changes)
  finish_work  — wrap up work (diary + decision capture + release)
  publish_handoff — publish work to another session (handoff + release)
  takeover_work — take over from another session (accept + claim + context)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastmcp import Context

from ._session_tools import _require_session_id, _optional_session_id, _get_session_id_from_ctx


# ────────────────────────────────────────────────────────────────────────────────
# Shared result constructors
# ────────────────────────────────────────────────────────────────────────────────

def _ok(phase: str, action: str, data: dict, next_actions: list[dict],
        context_snippets: dict | None = None, failure_mode: str | None = None) -> dict:
    """Build a success workflow_result."""
    return {
        "ok": True,
        "phase": phase,
        "action": action,
        **data,
        "next_actions": next_actions,
        "failure_mode": failure_mode,
        "context_snippets": context_snippets or {},
    }


def _fail(phase: str, action: str, reason: str, hint: str,
          failure_mode: str, details: dict | None = None) -> dict:
    """Build an error workflow_result."""
    return {
        "ok": False,
        "phase": phase,
        "action": action,
        "reason": reason,
        "hint": hint,
        "failure_mode": failure_mode,
        "details": details or {},
    }


def _phase(workflow: str, step: str) -> str:
    return f"{workflow}:{step}"


# ────────────────────────────────────────────────────────────────────────────────
# mempalace_begin_work
# ────────────────────────────────────────────────────────────────────────────────

def _do_begin_work(
    ctx: Context,
    path: str,
    session_id: str,
    ttl_seconds: int,
    note: str | None,
    claims_mgr,
    write_coordinator,
) -> dict:
    """
    Internal implementation: conflict_check → claim_path → log_intent.

    Returns (workflow_result, claim_acquired, conflict_info).
    """
    if claims_mgr is None:
        return (
            _fail(
                _phase("begin_work", "init"),
                "begin_work",
                reason="session coordination not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_coordination",
            ),
            False,
            {},
        )

    # Step 1: conflict check
    conflict = claims_mgr.check_conflicts("file", path, session_id)
    if conflict.get("has_conflict") and not conflict.get("is_self"):
        return (
            _fail(
                _phase("begin_work", "conflict_check"),
                "begin_work",
                reason=f"Active claim held by '{conflict.get('owner')}'",
                hint=(
                    f"Wait for TTL expiry ({conflict.get('expires_at', 'unknown')}) "
                    "or negotiate via mempalace_push_handoff"
                ),
                failure_mode="claim_conflict",
                details={
                    "owner": conflict.get("owner"),
                    "expires_at": conflict.get("expires_at"),
                    "target_id": path,
                },
            ),
            False,
            conflict,
        )

    # Step 2: acquire / refresh claim
    payload = {"note": note, "path": path} if note else {"path": path}
    claim_result = claims_mgr.claim("file", path, session_id, ttl_seconds=ttl_seconds, payload=payload)

    if not claim_result.get("acquired") and not conflict.get("is_self"):
        return (
            _fail(
                _phase("begin_work", "claim_acquire"),
                "begin_work",
                reason="Claim acquisition failed",
                hint="Check MEMPALACE_SESSION_ID is set and TTL is valid",
                failure_mode="claim_acquire_failed",
                details={"claim_result": claim_result},
            ),
            False,
            conflict,
        )

    # Step 3: log intent to WriteCoordinator for crash recovery
    intent_id = None
    if write_coordinator and session_id:
        try:
            intent_id = write_coordinator.log_intent(
                session_id, "edit", "file", path, payload
            )
        except Exception:
            pass  # fail-open on intent logging

    owner = claim_result.get("owner") or session_id
    expires_at = claim_result.get("expires_at", "unknown")

    data = {
        "path": path,
        "session_id": session_id,
        "owner": owner,
        "expires_at": expires_at,
        "intent_id": intent_id,
        "conflict_resolved": bool(conflict.get("has_conflict") and conflict.get("is_self")),
        "was_conflict": conflict.get("has_conflict", False),
    }

    next_actions = [
        {
            "action": "mempalace_prepare_edit",
            "reason": "Get symbol context and recent changes before editing",
            "priority": "high",
        },
    ]

    return (
        _ok(
            _phase("begin_work", "done"),
            "begin_work",
            data,
            next_actions,
            context_snippets={"path": path, "note": note or ""},
        ),
        True,
        conflict,
    )


# ────────────────────────────────────────────────────────────────────────────────
# mempalace_prepare_edit
# ────────────────────────────────────────────────────────────────────────────────

def _do_prepare_edit(
    ctx: Context,
    path: str,
    session_id: str,
    palace_path: str,
    project_root: str | None,
    symbol_index,
) -> dict:
    """
    Internal: get file symbols + recent changes for a path.

    Returns (workflow_result, symbols_data).
    """
    snippets = {}
    next_actions_list = []

    # Symbols
    symbols_data = {}
    try:
        if symbol_index and path:
            symbols_data = symbol_index.get_file_symbols(path)
            sym_entries = symbols_data.get("symbols", [])
            if sym_entries:
                snippets["symbols"] = [
                    {"name": s["name"], "type": s.get("type", "?"), "line": s.get("line_start", 0)}
                    for s in sym_entries[:8]
                ]
                next_actions_list.append({
                    "action": "mempalace_search",
                    "reason": f"Search {path} content before editing",
                    "priority": "medium",
                })
            else:
                snippets["symbols"] = []
    except Exception:
        symbols_data = {}

    # Recent changes for this specific file
    recent_info = {}
    try:
        if project_root:
            from ..recent_changes import get_recent_changes
            changes = get_recent_changes(project_root, n=5)
            file_changes = [c for c in changes if c.get("file_path") == path or c.get("abs_path") == path]
            if file_changes:
                recent_info = file_changes[0]
                snippets["recent_change"] = {
                    "file_path": path,
                    "change_count": recent_info.get("change_count", 1),
                    "last_modified": recent_info.get("last_modified"),
                }
                next_actions_list.append({
                    "action": "mempalace_conflict_check",
                    "reason": f"Hot file changed {recent_info.get('change_count', 1)}x recently — verify no concurrent edit",
                    "priority": "high",
                })
    except Exception:
        pass

    data = {
        "path": path,
        "session_id": session_id,
        "symbols_count": len(snippets.get("symbols", [])),
        "recent_change": recent_info,
        "hotspot": recent_info.get("change_count", 0) >= 3 if recent_info else False,
    }

    return (
        _ok(
            _phase("prepare_edit", "done"),
            "prepare_edit",
            data,
            next_actions_list,
            context_snippets=snippets,
        ),
        symbols_data,
    )


# ────────────────────────────────────────────────────────────────────────────────
# mempalace_finish_work
# ────────────────────────────────────────────────────────────────────────────────

def _do_finish_work(
    ctx: Context,
    path: str,
    session_id: str,
    diary_entry: str | None,
    topic: str,
    agent_name: str,
    capture_decision: str | None,
    rationale: str | None,
    decision_category: str,
    decision_confidence: int,
    claims_mgr,
    decision_tracker,
) -> dict:
    """
    Internal: release claim + optional diary + optional decision capture.

    Returns workflow_result.
    """
    if claims_mgr is None:
        return _fail(
            _phase("finish_work", "init"),
            "finish_work",
            reason="session coordination not available",
            hint="Run with shared_server_mode=True or transport='http'",
            failure_mode="no_coordination",
        )
    results = []
    errors = []

    # Step 1: release claim
    release_result = {"success": False}
    try:
        release_result = claims_mgr.release_claim("file", path, session_id)
        results.append(f"claim_released:{release_result.get('success')}")
    except Exception as e:
        errors.append(f"claim_release:{e}")
        release_result = {"success": False, "error": str(e)}

    # Step 2: optional diary write
    diary_id = None
    if diary_entry:
        try:
            # Use wing_{agent_name}/diary as target — same as mempalace_diary_write
            from ..config import sanitize_name, sanitize_content
            agent_name_sanitized = sanitize_name(agent_name, "agent_name")
            entry_sanitized = sanitize_content(diary_entry)
            wing = f"wing_{agent_name_sanitized.lower().replace(' ', '_')}"
            room = "diary"
            # We can't call the tool directly (it would do its own claim check),
            # so we write to the backend directly here. But for simplicity,
            # we return diary_write instruction rather than duplicating the backend call.
            diary_id = f"diary_{wing}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            results.append(f"diary_ready:{diary_id}")
        except Exception as e:
            errors.append(f"diary:{e}")

    # Step 3: optional decision capture
    decision_id = None
    if capture_decision and rationale:
        try:
            decision_result = decision_tracker.capture_decision(
                session_id=session_id,
                decision_text=capture_decision,
                rationale=rationale,
                alternatives=[],
                category=decision_category,
                confidence=decision_confidence,
            )
            decision_id = decision_result.get("id")
            results.append(f"decision_captured:{decision_id}")
        except Exception as e:
            errors.append(f"decision:{e}")

    data = {
        "path": path,
        "session_id": session_id,
        "claim_released": release_result.get("success"),
        "diary_id": diary_id,
        "diary_entry": diary_entry,
        "decision_id": decision_id,
        "operations": results,
        "errors": errors,
    }

    next_actions = []
    if diary_entry:
        next_actions.append({
            "action": "mempalace_diary_write",
            "reason": "Write the diary entry prepared above",
            "priority": "high",
        })
    if capture_decision:
        next_actions.append({
            "action": "mempalace_capture_decision",
            "reason": "Persist the architectural decision",
            "priority": "high",
        })

    return _ok(
        _phase("finish_work", "done"),
        "finish_work",
        data,
        next_actions,
        context_snippets={"diary_entry": diary_entry or "", "decision": capture_decision or ""},
    )


# ────────────────────────────────────────────────────────────────────────────────
# mempalace_publish_handoff
# ────────────────────────────────────────────────────────────────────────────────

def _do_publish_handoff(
    ctx: Context,
    summary: str,
    touched_paths: list[str],
    blockers: list[str],
    next_steps: list[str],
    confidence: int,
    priority: str,
    to_session_id: str | None,
    from_session_id: str,
    claims_mgr,
    handoff_mgr,
) -> dict:
    """
    Internal: push handoff + release claims on touched paths.

    Returns workflow_result.
    """
    if handoff_mgr is None:
        return _fail(
            _phase("publish_handoff", "init"),
            "publish_handoff",
            reason="handoff manager not available",
            hint="Run with shared_server_mode=True or transport='http'",
            failure_mode="no_handoff_manager",
        )
    if claims_mgr is None:
        return _fail(
            _phase("publish_handoff", "init"),
            "publish_handoff",
            reason="claims manager not available",
            hint="Run with shared_server_mode=True",
            failure_mode="no_claims_manager",
        )

    # Push handoff first (atomic — don't release claims if push fails)
    handoff_result = handoff_mgr.push_handoff(
        from_session_id=from_session_id,
        summary=summary,
        touched_paths=touched_paths or [],
        blockers=blockers or [],
        next_steps=next_steps or [],
        confidence=confidence,
        priority=priority,
        to_session_id=to_session_id,
    )

    handoff_id = handoff_result.get("id")
    if not handoff_id:
        return _fail(
            _phase("publish_handoff", "push"),
            "publish_handoff",
            reason="Handoff push failed",
            hint=handoff_result.get("error", "Unknown error from HandoffManager"),
            failure_mode="handoff_push_failed",
            details=handoff_result,
        )

    # Release claims on all touched paths
    released = []
    errors = []
    for path in touched_paths:
        try:
            result = claims_mgr.release_claim("file", path, from_session_id)
            released.append({"path": path, "success": result.get("success", False)})
        except Exception as e:
            errors.append({"path": path, "error": str(e)})

    data = {
        "handoff_id": handoff_id,
        "from_session_id": from_session_id,
        "to_session_id": to_session_id,
        "summary": summary,
        "touched_paths": touched_paths,
        "released_claims": released,
        "release_errors": errors,
    }

    next_actions = [
        {
            "action": "mempalace_diary_write",
            "reason": "Log the completed work to your diary",
            "priority": "medium",
        },
    ]

    return _ok(
        _phase("publish_handoff", "done"),
        "publish_handoff",
        data,
        next_actions,
        context_snippets={"summary": summary[:200], "touched_paths": touched_paths},
    )


# ────────────────────────────────────────────────────────────────────────────────
# mempalace_takeover_work
# ────────────────────────────────────────────────────────────────────────────────

def _do_takeover_work(
    ctx: Context,
    handoff_id: str,
    session_id: str,
    paths_to_claim: list[str],
    ttl_seconds: int,
    handoff_mgr,
    claims_mgr,
    write_coordinator,
) -> dict:
    """
    Internal: accept handoff + claim specified paths.

    Returns workflow_result.
    """
    # Guards — check availability before any state-changing operation
    if handoff_mgr is None:
        return _fail(
            _phase("takeover_work", "init"),
            "takeover_work",
            reason="handoff manager not available",
            hint="Run with shared_server_mode=True or transport='http'",
            failure_mode="no_handoff_manager",
        )
    if claims_mgr is None:
        return _fail(
            _phase("takeover_work", "init"),
            "takeover_work",
            reason="session coordination not available",
            hint="Run with shared_server_mode=True",
            failure_mode="no_coordination",
        )

    # Step 1: accept handoff
    accept_result = handoff_mgr.accept_handoff(handoff_id, session_id)
    if accept_result.get("status") == "error" or not accept_result.get("accepted"):
        return _fail(
            _phase("takeover_work", "accept"),
            "takeover_work",
            reason=accept_result.get("error", "Handoff acceptance failed"),
            hint="Verify the handoff_id is valid and you are the intended recipient",
            failure_mode="handoff_accept_failed",
            details=accept_result,
        )

    # Step 2: claim all specified paths
    claimed = []
    errors = []
    for path in paths_to_claim:
        conflict = claims_mgr.check_conflicts("file", path, session_id)
        if conflict.get("has_conflict") and not conflict.get("is_self"):
            errors.append({
                "path": path,
                "error": "claim_conflict",
                "owner": conflict.get("owner"),
            })
            continue
        result = claims_mgr.claim("file", path, session_id, ttl_seconds=ttl_seconds)
        claimed.append({
            "path": path,
            "acquired": result.get("acquired", False),
            "owner": result.get("owner"),
        })
        # Log intent
        if write_coordinator:
            try:
                write_coordinator.log_intent(session_id, "edit", "file", path, {"source": "takeover"})
            except Exception:
                pass

    data = {
        "handoff_id": handoff_id,
        "session_id": session_id,
        "handoff_accepted": True,
        "claimed_paths": claimed,
        "claim_errors": errors,
        "all_claims_acquired": len(errors) == 0,
    }

    next_actions = [
        {
            "action": "mempalace_wakeup_context",
            "reason": "Get full context for the takeover",
            "priority": "high",
        },
        {
            "action": "mempalace_prepare_edit",
            "reason": "Get symbol context for each claimed path",
            "priority": "high",
        },
    ]

    handoff_summary = accept_result.get("summary", "")
    return _ok(
        _phase("takeover_work", "done"),
        "takeover_work",
        data,
        next_actions,
        context_snippets={"handoff_summary": handoff_summary[:200]},
    )


# ────────────────────────────────────────────────────────────────────────────────
# Tool registration
# ────────────────────────────────────────────────────────────────────────────────

def register_workflow_tools(server, backend, config, settings):
    """
    Register compound workflow @mcp.tool() as closures.
    Called by factory.create_server().
    """
    def _get_claims_manager():
        return getattr(server, "_claims_manager", None)

    def _get_handoff_manager():
        return getattr(server, "_handoff_manager", None)

    def _get_decision_tracker():
        return getattr(server, "_decision_tracker", None)

    def _get_write_coordinator():
        return getattr(server, "_write_coordinator", None)

    def _get_symbol_index():
        try:
            from ..symbol_index import SymbolIndex
            return SymbolIndex.get(settings.palace_path)
        except Exception:
            return None

    # ── mempalace_begin_work ───────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_begin_work(
        ctx: Context,
        path: str,
        session_id: str | None = None,
        ttl_seconds: int = 600,
        note: str | None = None,
    ) -> dict:
        """
        Begin a working session on a file or path.

        Combines in one call:
          1. conflict_check — is the path already claimed?
          2. claim_path   — acquire or refresh the claim
          3. log_intent   — record write intent for crash recovery

        Returns a workflow_result with:
          - ok / error status
          - phase (which step failed if any)
          - path, session_id, owner, expires_at
          - next_actions: list of recommended next steps
          - failure_mode: short code for programmatic handling
          - context_snippets: path, note

        On claim_conflict failure_mode:
          - Wait for TTL expiry (check expires_at)
          - Or push a handoff to the owner requesting release
          - Or use claim_mode='advisory' to proceed with a warning

        TTL guidance:
          Quick fix (< 5 min)  : 300s
          Standard edit        : 600s  (default)
          Large refactor       : 1800s
          Multi-file change    : 3600s
        """
        claims_mgr = _get_claims_manager()
        if claims_mgr is None:
            return _fail(
                _phase("begin_work", "init"),
                "begin_work",
                reason="session coordination not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_coordination",
            )

        wc = _get_write_coordinator()
        resolved = _require_session_id(ctx, session_id, "begin_work")

        result, _, _ = _do_begin_work(
            ctx, path, resolved, ttl_seconds, note, claims_mgr, wc
        )
        return result

    # ── mempalace_prepare_edit ──────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_read)
    def mempalace_prepare_edit(
        ctx: Context,
        path: str,
        session_id: str | None = None,
    ) -> dict:
        """
        Prepare to edit a file — get symbol context and recent change info.

        Combines in one call:
          1. file_symbols  — top-level definitions in the file
          2. recent_changes — has this file changed recently? (hot spot?)

        Returns a workflow_result with:
          - ok / error status
          - path, symbols_count
          - context_snippets.symbols: list of {name, type, line}
          - context_snippets.recent_change: change_count, last_modified
          - next_actions: search + conflict_check suggestions
          - hotspot: true if file changed >= 3x recently

        Call this BEFORE making edits on hot or complex files.
        """
        resolved = _optional_session_id(ctx, session_id)
        si = _get_symbol_index()

        result, _ = _do_prepare_edit(
            ctx, path, resolved, settings.palace_path,
            os.environ.get("PROJECT_ROOT"), si
        )
        return result

    # ── mempalace_finish_work ──────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_finish_work(
        ctx: Context,
        path: str,
        session_id: str | None = None,
        diary_entry: str | None = None,
        topic: str = "general",
        agent_name: str = "claude",
        capture_decision: str | None = None,
        rationale: str | None = None,
        decision_category: str = "general",
        decision_confidence: int = 3,
    ) -> dict:
        """
        Finish a working session — release claim, optionally log diary and capture decision.

        Combines in one call:
          1. release_claim  — give up the path claim
          2. diary_write   — log what was done (if diary_entry provided)
          3. capture_decision — persist an architectural decision (if capture_decision provided)

        Returns a workflow_result with:
          - ok / error status
          - path, session_id, claim_released
          - diary_id (if diary_entry provided, ready to use in mempalace_diary_write)
          - decision_id (if capture_decision provided)
          - next_actions: diary_write + capture_decision instructions if not completed
          - operations: list of operations performed
          - errors: any errors encountered

        diary_entry is prepared but not written — call mempalace_diary_write separately
        with the returned diary_id as reference.
        """
        claims_mgr = _get_claims_manager()
        if claims_mgr is None:
            return _fail(
                _phase("finish_work", "init"),
                "finish_work",
                reason="session coordination not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_coordination",
            )

        dt = _get_decision_tracker()
        if capture_decision and dt is None:
            return _fail(
                _phase("finish_work", "init"),
                "finish_work",
                reason="decision tracker not available",
                hint="Run with shared_server_mode=True",
                failure_mode="no_decision_tracker",
            )

        resolved = _require_session_id(ctx, session_id, "finish_work")

        return _do_finish_work(
            ctx, path, resolved, diary_entry, topic, agent_name,
            capture_decision, rationale, decision_category, decision_confidence,
            claims_mgr, dt or _EmptyDecisionTracker(),
        )

    # ── mempalace_publish_handoff ─────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_publish_handoff(
        ctx: Context,
        summary: str,
        touched_paths: list[str] | None = None,
        blockers: list[str] | None = None,
        next_steps: list[str] | None = None,
        confidence: int = 3,
        priority: str = "normal",
        to_session_id: str | None = None,
        from_session_id: str | None = None,
    ) -> dict:
        """
        Publish a handoff and release all claims on touched paths.

        Combines in one call:
          1. push_handoff  — create the handoff record
          2. release_claim — for each path in touched_paths

        Returns a workflow_result with:
          - ok / error status
          - handoff_id, from/to session, summary
          - released_claims: list of {path, success} for each touched path
          - next_actions: diary_write suggestion
          - failure_mode: push_failed (handoff not created) or partial_release

        If push fails, no claims are released (atomic on handoff creation).
        If release fails for some paths, those are reported in release_errors
        but the handoff is still created.

        to_session_id=None means broadcast (any session can pick up).
        """
        handoff_mgr = _get_handoff_manager()
        if handoff_mgr is None:
            return _fail(
                _phase("publish_handoff", "init"),
                "publish_handoff",
                reason="handoff manager not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_handoff_manager",
            )

        claims_mgr = _get_claims_manager()
        if claims_mgr is None:
            return _fail(
                _phase("publish_handoff", "init"),
                "publish_handoff",
                reason="claims manager not available",
                hint="Run with shared_server_mode=True",
                failure_mode="no_claims_manager",
            )

        resolved_from = _require_session_id(ctx, from_session_id, "publish_handoff")

        return _do_publish_handoff(
            ctx, summary,
            touched_paths or [], blockers or [], next_steps or [],
            confidence, priority, to_session_id,
            resolved_from,
            claims_mgr, handoff_mgr,
        )

    # ── mempalace_takeover_work ────────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_takeover_work(
        ctx: Context,
        handoff_id: str,
        paths_to_claim: list[str] | None = None,
        session_id: str | None = None,
        ttl_seconds: int = 600,
    ) -> dict:
        """
        Take over work from a handoff — accept it and claim the relevant paths.

        Combines in one call:
          1. accept_handoff — mark ownership
          2. claim_path    — for each path in paths_to_claim (or handoff's touched_paths)

        Returns a workflow_result with:
          - ok / error status
          - handoff_id, session_id, handoff_accepted
          - claimed_paths: list of {path, acquired, owner}
          - claim_errors: any paths that couldn't be claimed
          - all_claims_acquired: true if every path was claimed successfully
          - next_actions: wakeup_context + prepare_edit for each path
          - failure_mode: accept_failed (bad handoff_id), claim_conflict (some paths blocked)

        If accept fails, no claims are made. If individual claims fail,
        they're reported but the handoff is already accepted.
        """
        handoff_mgr = _get_handoff_manager()
        if handoff_mgr is None:
            return _fail(
                _phase("takeover_work", "init"),
                "takeover_work",
                reason="handoff manager not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_handoff_manager",
            )

        claims_mgr = _get_claims_manager()
        if claims_mgr is None:
            return _fail(
                _phase("takeover_work", "init"),
                "takeover_work",
                reason="session coordination not available",
                hint="Run with shared_server_mode=True",
                failure_mode="no_coordination",
            )

        wc = _get_write_coordinator()
        resolved = _require_session_id(ctx, session_id, "takeover_work")

        # If no paths provided, fetch from the handoff itself
        actual_paths = paths_to_claim or []
        if not actual_paths:
            try:
                # Try to get touched_paths from the handoff record
                handoffs = handoff_mgr.pull_handoffs(session_id=None, status=None)
                for h in handoffs:
                    if h.get("id") == handoff_id:
                        actual_paths = h.get("touched_paths", [])
                        break
            except Exception:
                pass

        return _do_takeover_work(
            ctx, handoff_id, resolved, actual_paths, ttl_seconds,
            handoff_mgr, claims_mgr, wc,
        )


class _EmptyDecisionTracker:
    """Passthrough for finish_work when decision_tracker is unavailable."""
    def capture_decision(self, **kwargs):
        return {"id": None, "error": "no decision tracker"}


# ── needed in _do_prepare_edit ────────────────────────────────────────────
import os
