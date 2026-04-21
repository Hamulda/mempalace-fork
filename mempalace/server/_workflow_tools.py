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

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastmcp import Context

from ._session_tools import _require_session_id, _optional_session_id, _get_session_id_from_ctx


# ────────────────────────────────────────────────────────────────────────────────
# Project root resolution — canonical source of truth
# ────────────────────────────────────────────────────────────────────────────────

def _find_git_root(start_path: str) -> str | None:
    """
    Find the git repository root by walking up from start_path.

    Returns the containing git repo root, or None if no .git directory found.
    This is the canonical way to derive project_root from a file path — no env needed.
    """
    try:
        current = Path(start_path).expanduser().resolve()
        if current.is_file():
            current = current.parent
        # Walk up to find .git
        for parent in [current] + list(current.parents):
            if (parent / ".git").is_dir():
                return str(parent)
    except Exception:
        pass
    return None


def _resolve_project_root(explicit: str | None, palace_path: str, file_path: str | None = None) -> str | None:
    """
    Resolve project_root with explicit priority and deterministic fallback.

    Resolution order:
    1. explicit parameter (caller-provided, most specific)
    2. git root from file_path (when editing a known file)
    3. git root from palace_path (palace lives inside a project)
    4. None (caller handles missing project context gracefully)

    No env dependency — project_root is always derived or explicitly provided.
    """
    if explicit:
        return explicit
    if file_path:
        git_root = _find_git_root(file_path)
        if git_root:
            return git_root
    git_root = _find_git_root(palace_path)
    if git_root:
        return git_root
    return None


# ────────────────────────────────────────────────────────────────────────────────
# Shared result constructors
# ────────────────────────────────────────────────────────────────────────────────

def _ok(phase: str, action: str, data: dict, next_actions: list[dict],
        context_snippets: dict | None = None, failure_mode: str | None = None,
        workflow_state: dict | None = None) -> dict:
    """Build a success workflow_result."""
    return {
        "ok": True,
        "phase": phase,
        "action": action,
        **data,
        "next_actions": next_actions,
        "failure_mode": failure_mode,
        "context_snippets": context_snippets or {},
        "workflow_state": workflow_state or {},
    }


def _fail(phase: str, action: str, reason: str, hint: str,
          failure_mode: str, details: dict | None = None,
          workflow_state: dict | None = None,
          next_actions: list[dict] | None = None) -> dict:
    """Build an error workflow_result."""
    return {
        "ok": False,
        "phase": phase,
        "action": action,
        "reason": reason,
        "hint": hint,
        "failure_mode": failure_mode,
        "details": details or {},
        "workflow_state": workflow_state or {},
        "next_actions": next_actions or [],
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
                workflow_state={
                    "current_phase": "unavailable",
                    "next_phase": None,
                    "next_tool": None,
                    "conflict_status": "unknown",
                    "handoff_pending": False,
                },
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
                workflow_state={
                    "current_phase": "blocked",
                    "next_phase": "negotiate",
                    "next_tool": "mempalace_push_handoff",
                    "conflict_status": "other_claim",
                    "handoff_pending": False,
                },
                next_actions=[{
                    "action": "mempalace_push_handoff",
                    "reason": "File claimed by another session — negotiate or wait for TTL expiry",
                    "priority": "high",
                }],
            ),
            False,
            conflict,
        )

    # Step 2: capture baseline before claim (edit verification baseline)
    baseline: dict | None = None
    try:
        stat = os.stat(path)
        baseline = {
            "mtime_sec": stat.st_mtime,
            "size": stat.st_size,
        }
    except OSError:
        pass  # file may not exist yet — unknown baseline

    # Step 3: acquire / refresh claim
    payload = {"note": note, "path": path, "edit_baseline": baseline} if note else {"path": path, "edit_baseline": baseline}
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
                workflow_state={
                    "current_phase": "blocked",
                    "next_phase": "retry",
                    "next_tool": "mempalace_begin_work",
                    "conflict_status": "unknown",
                    "handoff_pending": False,
                },
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
    is_self = bool(conflict.get("is_self"))

    data = {
        "path": path,
        "session_id": session_id,
        "owner": owner,
        "expires_at": expires_at,
        "intent_id": intent_id,
        "conflict_resolved": bool(conflict.get("has_conflict") and conflict.get("is_self")),
        "was_conflict": conflict.get("has_conflict", False),
        "edit_baseline": baseline,  # mtime/size at begin_work — used by finish_work to detect actual edits
    }

    next_actions = [
        {
            "action": "mempalace_prepare_edit",
            "reason": "Get symbol context and recent changes before editing",
            "priority": "high",
            "skill": "prepare-edit",
        },
    ]

    return (
        _ok(
            _phase("begin_work", "done"),
            "begin_work",
            data,
            next_actions,
            context_snippets={"path": path, "note": note or ""},
            workflow_state={
                "current_phase": "claim_acquired",
                "next_phase": "prepare",
                "next_tool": "mempalace_prepare_edit",
                "conflict_status": "self_claim" if is_self else "none",
                "handoff_pending": False,
                "pending_edits": {
                    "paths": [path],
                    "expected_outcome": note or "file edited",
                    "claim_ttl_seconds": ttl_seconds,
                    "edits_must_happen_before": expires_at,
                    "edit_baseline": baseline,
                },
            },
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
    claims_mgr,
    preview_mode: str = "slice",
) -> tuple[dict, dict, dict | None]:
    """
    Internal: file_symbols + recent_changes + auto conflict_check + optional content preview.

    Combines prepare_edit with conflict_check in one call — the model
    gets symbol context AND verification that no concurrent edit is in progress,
    without a separate explicit conflict_check call.

    preview_mode controls content preview:
      - "slice"  (default): read ~40 lines around the first symbol (M1-friendly, ≈3KB)
      - "none"   : symbols only, no file read (backward-compatible, zero extra I/O)
    """
    snippets = {}
    next_actions_list = []

    # Step 1: Auto conflict check (critical for hot spots)
    conflict_info = {}
    if claims_mgr:
        conflict_info = claims_mgr.check_conflicts("file", path, session_id or "")
        if conflict_info.get("has_conflict") and not conflict_info.get("is_self"):
            # File is actively claimed by another session
            return (
                _fail(
                    _phase("prepare_edit", "conflict_check"),
                    "prepare_edit",
                    reason=f"Active claim held by '{conflict_info.get('owner')}'",
                    hint=f"Wait for TTL expiry ({conflict_info.get('expires_at', 'unknown')}) "
                         "or negotiate via mempalace_push_handoff before editing",
                    failure_mode="claim_conflict",
                    details={
                        "owner": conflict_info.get("owner"),
                        "expires_at": conflict_info.get("expires_at"),
                        "target_id": path,
                    },
                    workflow_state={
                        "current_phase": "blocked",
                        "next_phase": "negotiate",
                        "next_tool": "mempalace_push_handoff",
                        "conflict_status": "other_claim",
                        "handoff_pending": False,
                    },
                    next_actions=[{
                        "action": "mempalace_push_handoff",
                        "reason": "File claimed by another session — negotiate or wait for TTL expiry",
                        "priority": "high",
                    }],
                ),
                {},
                None,
            )

    # Step 2: Symbols
    symbols_data = {}
    first_symbol_line = None
    try:
        if symbol_index and path:
            symbols_data = symbol_index.get_file_symbols(path)
            sym_entries = symbols_data.get("symbols", [])
            if sym_entries:
                snippets["symbols"] = [
                    {"name": s["name"], "type": s.get("type", "?"), "line": s.get("line_start", 0)}
                    for s in sym_entries[:8]
                ]
                first_symbol_line = sym_entries[0].get("line_start", 1)
            else:
                snippets["symbols"] = []
    except Exception:
        symbols_data = {}

    # Step 3: Recent changes / hot spot detection
    recent_info = {}
    hotspot_detected = False
    try:
        if project_root:
            from ..recent_changes import get_recent_changes
            changes = get_recent_changes(project_root, n=5)
            file_changes = [c for c in changes if c.get("file_path") == path or c.get("abs_path") == path]
            if file_changes:
                recent_info = file_changes[0]
                hotspot_detected = recent_info.get("change_count", 0) >= 3
                snippets["recent_change"] = {
                    "file_path": path,
                    "change_count": recent_info.get("change_count", 1),
                    "last_modified": recent_info.get("last_modified"),
                }
    except Exception:
        pass

    # Step 4: Content preview (optional, controlled by preview_mode)
    file_slice = None
    if preview_mode == "slice" and first_symbol_line is not None:
        try:
            from pathlib import Path
            content = Path(path).read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
            total = len(lines)
            # Slice: 20 lines before first symbol, first symbol, 20 lines after
            # Clamp to file boundaries
            start = max(0, first_symbol_line - 21)  # 0-indexed, so -21 means 20 before (1-indexed line)
            end = min(total, first_symbol_line + 20)
            if end > start:
                slice_lines = lines[start:end]
                file_slice = {
                    "total_lines": total,
                    "slice_start": start + 1,   # 1-indexed for humans
                    "slice_end": end,
                    "pre_context": slice_lines[:20],
                    "symbol_context": slice_lines[20:],
                    "has_pre": start > 0,
                    "has_post": end < total,
                    "line_count": end - start,
                }
                snippets["file_slice"] = {
                    "total_lines": total,
                    "slice_start": start + 1,
                    "slice_end": end,
                    "has_pre": start > 0,
                    "has_post": end < total,
                }
        except Exception:
            pass

    # Step 4: Build next_actions
    if hotspot_detected:
        next_actions_list.append({
            "action": "mempalace_search",
            "reason": f"Hot file (≥3 changes) — understand current content before editing",
            "priority": "high",
        })
    else:
        next_actions_list.append({
            "action": "mempalace_search",
            "reason": f"Search {path} content before editing",
            "priority": "medium",
        })

    data = {
        "path": path,
        "session_id": session_id,
        "symbols_count": len(snippets.get("symbols", [])),
        "recent_change": recent_info,
        "hotspot": hotspot_detected,
        "conflict_verified": True,
        "preview_mode": preview_mode,
    }

    return (
        _ok(
            _phase("prepare_edit", "done"),
            "prepare_edit",
            data,
            next_actions_list,
            context_snippets=snippets,
            workflow_state={
                "current_phase": "context_ready",
                "next_phase": "edit",
                "next_tool": "MODEL_ACTION:edit",
                "conflict_status": "hotspot" if hotspot_detected else "none",
                "handoff_pending": False,
            },
        ),
        symbols_data,
        file_slice,  # may be None
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
    backend,
) -> dict:
    """
    Internal: release claim + optional diary write + optional decision capture.

    diary_entry is written immediately via backend — no separate call needed.
    """
    if claims_mgr is None:
        return _fail(
            _phase("finish_work", "init"),
            "finish_work",
            reason="session coordination not available",
            hint="Run with shared_server_mode=True or transport='http'",
            failure_mode="no_coordination",
            workflow_state={
                "current_phase": "unavailable",
                "next_phase": None,
                "next_tool": None,
                "conflict_status": "unknown",
                "handoff_pending": False,
            },
        )
    results = []
    errors = []

    # Step 1: extract baseline from claim BEFORE releasing (edit verification needs it)
    baseline: dict | None = None
    try:
        claim = claims_mgr.get_claim("file", path)
        if claim:
            baseline = claim.get("payload", {}).get("edit_baseline")
    except Exception:
        pass

    # Step 1b: release claim
    release_result = {"success": False}
    try:
        release_result = claims_mgr.release_claim("file", path, session_id)
        results.append(f"claim_released:{release_result.get('success')}")
    except Exception as e:
        errors.append(f"claim_release:{e}")
        release_result = {"success": False, "error": str(e)}

    # Step 1c: edit verification — detect whether file was actually changed
    # Best-effort: compare current mtime/size against baseline captured at begin_work.
    edit_verification = {"status": "unknown", "edited": False}
    try:
        if baseline:
            current_stat = os.stat(path)
            edited = (
                current_stat.st_mtime != baseline.get("mtime_sec")
                or current_stat.st_size != baseline.get("size")
            )
            edit_verification = {
                "status": "verified",
                "edited": edited,
                "baseline": baseline,
                "final": {"mtime_sec": current_stat.st_mtime, "size": current_stat.st_size},
            }
        else:
            edit_verification = {"status": "no_baseline", "edited": False}
    except OSError:
        edit_verification = {"status": "file_missing", "edited": False}
    except Exception as e:
        edit_verification = {"status": "error", "edited": False, "error": str(e)}

    # Step 2: optional diary write — write immediately to backend
    diary_id = None
    if diary_entry and backend:
        try:
            from ..config import sanitize_name, sanitize_content
            agent_name_sanitized = sanitize_name(agent_name, "agent_name")
            entry_sanitized = sanitize_content(diary_entry)
            wing = f"wing_{agent_name_sanitized.lower().replace(' ', '_')}"
            room = "diary"

            col = backend.get_collection()
            now = datetime.now(timezone.utc)
            diary_id = f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S')}"
            entry_timestamp = now.isoformat() + "Z"

            col.upsert(
                ids=[diary_id],
                documents=[entry_sanitized],
                metadatas=[{
                    "wing": wing, "room": room, "source_file": path,
                    "chunk_index": 0, "added_by": agent_name, "agent_id": agent_name,
                    "entities": "[]", "timestamp": entry_timestamp,
                    "origin_type": "diary_entry", "is_latest": True,
                    "supersedes_id": "", "topic": topic,
                }],
            )
            results.append(f"diary_written:{diary_id}")
        except Exception as e:
            errors.append(f"diary_write:{e}")
            diary_id = None
    elif diary_entry:
        # No backend — prepare diary_id but can't write
        try:
            from ..config import sanitize_name, sanitize_content
            agent_name_sanitized = sanitize_name(agent_name, "agent_name")
            wing = f"wing_{agent_name_sanitized.lower().replace(' ', '_')}"
            diary_id = f"diary_{wing}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            results.append(f"diary_prepared:{diary_id}")
        except Exception as e:
            errors.append(f"diary_prep:{e}")

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
        "edit_verification": edit_verification,
        "diary_id": diary_id,
        "diary_entry": diary_entry,
        "decision_id": decision_id,
        "operations": results,
        "errors": errors,
    }

    next_actions = []
    if capture_decision and not decision_id:
        next_actions.append({
            "action": "mempalace_capture_decision",
            "reason": "Persist the architectural decision",
            "priority": "high",
        })
    if diary_entry and not diary_id:
        next_actions.append({
            "action": "mempalace_diary_write",
            "reason": "Write the diary entry prepared above",
            "priority": "medium",
        })

    return _ok(
        _phase("finish_work", "done"),
        "finish_work",
        data,
        next_actions,
        context_snippets={"diary_entry": diary_entry or "", "decision": capture_decision or ""},
        workflow_state={
            "current_phase": "finished",
            "next_phase": None,
            "next_tool": None,
            "conflict_status": "none",
            "handoff_pending": False,
        },
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
            workflow_state={
                "current_phase": "unavailable",
                "next_phase": None,
                "next_tool": None,
                "conflict_status": "unknown",
                "handoff_pending": False,
            },
        )
    if claims_mgr is None:
        return _fail(
            _phase("publish_handoff", "init"),
            "publish_handoff",
            reason="claims manager not available",
            hint="Run with shared_server_mode=True",
            failure_mode="no_claims_manager",
            workflow_state={
                "current_phase": "unavailable",
                "next_phase": None,
                "next_tool": None,
                "conflict_status": "unknown",
                "handoff_pending": False,
            },
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
            workflow_state={
                "current_phase": "blocked",
                "next_phase": "retry",
                "next_tool": "mempalace_publish_handoff",
                "conflict_status": "none",
                "handoff_pending": False,
            },
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
        workflow_state={
            "current_phase": "published",
            "next_phase": None,
            "next_tool": "mempalace_diary_write",
            "conflict_status": "none",
            "handoff_pending": False,
        },
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
            workflow_state={
                "current_phase": "unavailable",
                "next_phase": None,
                "next_tool": None,
                "conflict_status": "unknown",
                "handoff_pending": False,
            },
        )
    if claims_mgr is None:
        return _fail(
            _phase("takeover_work", "init"),
            "takeover_work",
            reason="session coordination not available",
            hint="Run with shared_server_mode=True",
            failure_mode="no_coordination",
            workflow_state={
                "current_phase": "unavailable",
                "next_phase": None,
                "next_tool": None,
                "conflict_status": "unknown",
                "handoff_pending": False,
            },
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
            workflow_state={
                "current_phase": "blocked",
                "next_phase": "retry",
                "next_tool": "mempalace_takeover_work",
                "conflict_status": "none",
                "handoff_pending": False,
            },
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
        workflow_state={
            "current_phase": "takeover",
            "next_phase": "prepare",
            "next_tool": "mempalace_wakeup_context",
            "conflict_status": "none",
            "handoff_pending": False,
        },
    )


# ────────────────────────────────────────────────────────────────────────────────
# mempalace_begin_work_batch
# ────────────────────────────────────────────────────────────────────────────────

def _do_begin_work_batch(
    ctx: Context,
    paths: list[str],
    session_id: str,
    ttl_seconds: int,
    note: str | None,
    claims_mgr,
    write_coordinator,
) -> dict:
    """
    Atomic multi-file claim: all-or-nothing conflict check + claim for a file set.

    Checks conflicts on ALL paths first. If any path is blocked by another
    session, the entire batch fails — no partial state is created.

    Returns (workflow_result, all_acquired, conflict_map).
    """
    if claims_mgr is None:
        return (
            _fail(
                _phase("begin_work_batch", "init"),
                "begin_work_batch",
                reason="session coordination not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_coordination",
                workflow_state={
                    "current_phase": "unavailable",
                    "next_phase": None,
                    "next_tool": None,
                    "conflict_status": "unknown",
                    "handoff_pending": False,
                },
            ),
            False,
            {},
        )

    # Step 1: capture baselines for all paths before claims (edit verification)
    baselines: dict[str, dict] = {}
    for path in paths:
        try:
            stat = os.stat(path)
            baselines[path] = {"mtime_sec": stat.st_mtime, "size": stat.st_size}
        except OSError:
            baselines[path] = None  # file may not exist yet

    # Step 2: conflict check on ALL paths first (all-or-nothing)
    conflict_map: dict[str, dict] = {}
    blocked_paths: list[dict] = []
    for path in paths:
        conflict = claims_mgr.check_conflicts("file", path, session_id)
        conflict_map[path] = conflict
        if conflict.get("has_conflict") and not conflict.get("is_self"):
            blocked_paths.append({
                "path": path,
                "owner": conflict.get("owner"),
                "expires_at": conflict.get("expires_at"),
            })

    if blocked_paths:
        return (
            _fail(
                _phase("begin_work_batch", "conflict_check"),
                "begin_work_batch",
                reason=f"{len(blocked_paths)} path(s) blocked by other sessions",
                hint="Wait for TTL expiry or negotiate via mempalace_push_handoff",
                failure_mode="batch_claim_conflict",
                details={"blocked_paths": blocked_paths},
                workflow_state={
                    "current_phase": "blocked",
                    "next_phase": "negotiate",
                    "next_tool": "mempalace_push_handoff",
                    "conflict_status": "other_claim",
                    "handoff_pending": False,
                },
                next_actions=[{
                    "action": "mempalace_push_handoff",
                    "reason": f"{len(blocked_paths)} file(s) claimed by other sessions — negotiate or wait",
                    "priority": "high",
                }],
            ),
            False,
            conflict_map,
        )

    # Step 2: acquire claims on ALL paths
    acquired_paths: list[dict] = []
    failed_paths: list[dict] = []
    for path in paths:
        baseline = baselines.get(path)
        payload = {"note": note, "path": path, "edit_baseline": baseline} if note else {"path": path, "edit_baseline": baseline}
        claim_result = claims_mgr.claim("file", path, session_id, ttl_seconds=ttl_seconds, payload=payload)
        if claim_result.get("acquired"):
            acquired_paths.append({"path": path, "owner": session_id, "expires_at": claim_result.get("expires_at")})
        else:
            failed_paths.append({"path": path, "owner": claim_result.get("owner")})

    # If any failed, fail the whole batch (rollback acquired claims)
    if failed_paths:
        for p in acquired_paths:
            try:
                claims_mgr.release_claim("file", p["path"], session_id)
            except Exception:
                pass
        return (
            _fail(
                _phase("begin_work_batch", "claim_acquire"),
                "begin_work_batch",
                reason="Some paths could not be acquired",
                hint="Check session health and retry",
                failure_mode="batch_partial_failure",
                details={
                    "acquired": acquired_paths,
                    "failed": failed_paths,
                },
                workflow_state={
                    "current_phase": "blocked",
                    "next_phase": "retry",
                    "next_tool": "mempalace_begin_work_batch",
                    "conflict_status": "unknown",
                    "handoff_pending": False,
                },
            ),
            False,
            conflict_map,
        )

    # Step 3: log batch intent to WriteCoordinator
    intent_id = None
    if write_coordinator and session_id:
        try:
            intent_id = write_coordinator.log_intent(
                session_id, "edit_batch", "file", "|".join(paths),
                {"note": note, "paths": paths}
            )
        except Exception:
            pass  # fail-open on intent logging

    expires_at = acquired_paths[0]["expires_at"] if acquired_paths else "unknown"

    data = {
        "paths": paths,
        "session_id": session_id,
        "owner": session_id,
        "expires_at": expires_at,
        "intent_id": intent_id,
        "edit_baselines": baselines,  # mtime/size per path at begin_work — used by finish_work_batch
    }

    next_actions = [{
        "action": "mempalace_prepare_edit",
        "reason": "Get symbol context for each claimed file before editing",
        "priority": "high",
    }]

    return (
        _ok(
            _phase("begin_work_batch", "done"),
            "begin_work_batch",
            data,
            next_actions,
            context_snippets={"paths": paths, "note": note or "", "count": len(paths)},
            workflow_state={
                "current_phase": "claim_acquired",
                "next_phase": "prepare",
                "next_tool": "mempalace_prepare_edit",
                "conflict_status": "none",
                "handoff_pending": False,
                "pending_edits": {
                    "paths": paths,
                    "expected_outcome": note or f"{len(paths)} files edited",
                    "claim_ttl_seconds": ttl_seconds,
                    "edits_must_happen_before": expires_at,
                    "edit_baselines": baselines,  # {path: {mtime_sec, size} | None}
                },
            },
        ),
        True,
        conflict_map,
    )


# ────────────────────────────────────────────────────────────────────────────────
# mempalace_finish_work_batch
# ────────────────────────────────────────────────────────────────────────────────

def _do_finish_work_batch(
    ctx: Context,
    paths: list[str],
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
    backend,
) -> dict:
    """
    Release all claims in paths + optionally write one diary entry + optionally
    capture one decision for the batch.

    All-or-nothing release attempt: tries to release each path, collects results.
    Diary is written once for the entire batch (not per-file).
    """
    if claims_mgr is None:
        return _fail(
            _phase("finish_work_batch", "init"),
            "finish_work_batch",
            reason="session coordination not available",
            hint="Run with shared_server_mode=True or transport='http'",
            failure_mode="no_coordination",
            workflow_state={
                "current_phase": "unavailable",
                "next_phase": None,
                "next_tool": None,
                "conflict_status": "unknown",
                "handoff_pending": False,
            },
        )

    results = []
    errors = []

    # Step 1: extract baselines from claims BEFORE releasing (edit verification needs them)
    baselines: dict[str, dict | None] = {}
    for path in paths:
        try:
            claim = claims_mgr.get_claim("file", path)
            baselines[path] = claim.get("payload", {}).get("edit_baseline") if claim else None
        except Exception:
            baselines[path] = None

    # Step 1b: release all claims
    released: list[dict] = []
    for path in paths:
        try:
            release_result = claims_mgr.release_claim("file", path, session_id)
            released.append({"path": path, "success": release_result.get("success")})
            results.append(f"claim_released:{path}:{release_result.get('success')}")
        except Exception as e:
            errors.append(f"claim_release:{path}:{e}")
            released.append({"path": path, "success": False, "error": str(e)})

    # Step 1c: edit verification — detect whether files were actually changed
    # Best-effort: compare current mtime/size against baselines captured at begin_work.
    edit_verifications: dict[str, dict] = {}
    for path in paths:
        ev = {"status": "unknown", "edited": False}
        baseline = baselines.get(path)
        try:
            if baseline:
                current_stat = os.stat(path)
                edited = (
                    current_stat.st_mtime != baseline.get("mtime_sec")
                    or current_stat.st_size != baseline.get("size")
                )
                ev = {
                    "status": "verified",
                    "edited": edited,
                    "baseline": baseline,
                    "final": {"mtime_sec": current_stat.st_mtime, "size": current_stat.st_size},
                }
            else:
                ev = {"status": "no_baseline", "edited": False}
        except OSError:
            ev = {"status": "file_missing", "edited": False}
        except Exception as e:
            ev = {"status": "error", "edited": False, "error": str(e)}
        edit_verifications[path] = ev

    # Step 2: write one diary entry for the batch
    diary_id = None
    if diary_entry and backend:
        try:
            from ..config import sanitize_name, sanitize_content
            agent_name_sanitized = sanitize_name(agent_name, "agent_name")
            entry_sanitized = sanitize_content(diary_entry)
            wing = f"wing_{agent_name_sanitized.lower().replace(' ', '_')}"
            room = "diary"

            col = backend.get_collection()
            now = datetime.now(timezone.utc)
            diary_id = f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S')}"
            entry_timestamp = now.isoformat() + "Z"

            col.upsert(
                ids=[diary_id],
                documents=[entry_sanitized],
                metadatas=[{
                    "wing": wing, "room": room,
                    "source_file": "|".join(paths),
                    "chunk_index": 0, "added_by": agent_name, "agent_id": agent_name,
                    "entities": "[]", "timestamp": entry_timestamp,
                    "origin_type": "diary_entry_batch", "is_latest": True,
                    "supersedes_id": "", "topic": topic,
                }],
            )
            results.append(f"diary_written:{diary_id}")
        except Exception as e:
            errors.append(f"diary_write:{e}")
            diary_id = None
    elif diary_entry:
        try:
            from ..config import sanitize_name, sanitize_content
            agent_name_sanitized = sanitize_name(agent_name, "agent_name")
            wing = f"wing_{agent_name_sanitized.lower().replace(' ', '_')}"
            diary_id = f"diary_{wing}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            results.append(f"diary_prepared:{diary_id}")
        except Exception as e:
            errors.append(f"diary_prep:{e}")

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
        "paths": paths,
        "session_id": session_id,
        "released_claims": released,
        "release_errors": errors,
        "edit_verifications": edit_verifications,  # {path: {status, edited, baseline, final}}
        "diary_id": diary_id,
        "diary_entry": diary_entry,
        "decision_id": decision_id,
        "operations": results,
        "errors": errors,
    }

    next_actions = []
    if capture_decision and not decision_id:
        next_actions.append({
            "action": "mempalace_capture_decision",
            "reason": "Persist the architectural decision",
            "priority": "high",
        })

    return _ok(
        _phase("finish_work_batch", "done"),
        "finish_work_batch",
        data,
        next_actions,
        context_snippets={"diary_entry": diary_entry or "", "paths": paths},
        workflow_state={
            "current_phase": "finished",
            "next_phase": None,
            "next_tool": None,
            "conflict_status": "none",
            "handoff_pending": False,
            "pending_edits": None,
        },
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
        preview_mode: str = "slice",
    ) -> dict:
        """
        Prepare to edit a file — get symbol context, recent changes, AND verify no concurrent edit.

        Combines in one call:
          1. conflict_check  — is another session actively editing? (blocks if yes)
          2. file_symbols   — top-level definitions in the file
          3. recent_changes — has this file changed recently? (hot spot?)
          4. file_slice     — ~40 lines around the first symbol (when preview_mode="slice")

        preview_mode controls content preview:
          - "slice" (default): read ~40 lines around the first symbol (M1-friendly, ≈3KB)
          - "none"  : symbols only, no file read (backward-compatible, zero extra I/O)

        Returns a workflow_result with:
          - ok / error status
          - path, symbols_count, hotspot, conflict_verified, preview_mode
          - context_snippets.symbols: list of {name, type, line}
          - context_snippets.recent_change: change_count, last_modified
          - context_snippets.file_slice: {total_lines, slice_start, slice_end, has_pre, has_post}
          - next_actions: search suggestions
          - failure_mode: claim_conflict if another session holds an active claim

        Call this BEFORE making edits on hot or complex files.
        """
        claims_mgr = _get_claims_manager()
        resolved = _optional_session_id(ctx, session_id)
        si = _get_symbol_index()

        result, _, _ = _do_prepare_edit(
            ctx, path, resolved, settings.palace_path,
            _resolve_project_root(None, settings.palace_path, path), si,
            claims_mgr,
            preview_mode=preview_mode,
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
          2. diary_write   — log what was done (if diary_entry provided, written immediately)
          3. capture_decision — persist an architectural decision (if capture_decision provided)

        Returns a workflow_result with:
          - ok / error status
          - path, session_id, claim_released
          - diary_id (if diary_entry provided, diary IS written — no follow-up call needed)
          - decision_id (if capture_decision provided)
          - next_actions: capture_decision reminder if not completed
          - operations: list of operations performed
          - errors: any errors encountered

        Unlike before, diary_entry is written immediately — no separate mempalace_diary_write call needed.
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
            backend,
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

    # ── mempalace_begin_work_batch ────────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_begin_work_batch(
        ctx: Context,
        paths: list[str],
        session_id: str | None = None,
        ttl_seconds: int = 1800,
        note: str | None = None,
    ) -> dict:
        """
        Begin a working session on multiple files atomically — all-or-nothing.

        Use for multi-file refactors where all files must be claimed together.
        If any path is blocked by another session, the ENTIRE batch fails
        (no partial state created).

        Combines in one call:
          1. conflict_check on ALL paths — if any blocked, all fail together
          2. claim_path for ALL paths — atomic acquisition
          3. log_intent for the batch

        Returns a workflow_result with:
          - ok / error status
          - phase, paths, session_id, owner, expires_at
          - failure_mode: batch_claim_conflict (some paths blocked),
                          batch_partial_failure (claim acquire failed)
          - next_actions: prepare_edit per file
          - pending_edits: paths + deadline in workflow_state

        TTL guidance for batch:
          Quick multi-file (< 10 min) : 900s
          Standard refactor           : 1800s (default)
          Large cross-module change   : 3600s
        """
        claims_mgr = _get_claims_manager()
        if claims_mgr is None:
            return _fail(
                _phase("begin_work_batch", "init"),
                "begin_work_batch",
                reason="session coordination not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_coordination",
            )

        wc = _get_write_coordinator()
        resolved = _require_session_id(ctx, session_id, "begin_work_batch")

        result, _, _ = _do_begin_work_batch(
            ctx, paths, resolved, ttl_seconds, note, claims_mgr, wc
        )
        return result

    # ── mempalace_finish_work_batch ───────────────────────────────────────

    @server.tool(timeout=settings.timeout_write)
    def mempalace_finish_work_batch(
        ctx: Context,
        paths: list[str],
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
        Finish a multi-file working session — release all claims + batch diary.

        Use after begin_work_batch when all edits are complete.

        Combines in one call:
          1. release_claim for each path in paths
          2. diary_write for the batch (single entry covering all paths)
          3. capture_decision (optional)

        Returns a workflow_result with:
          - ok / error status
          - paths, released_claims, diary_id (written immediately)
          - decision_id (if capture_decision provided)
          - next_actions: capture_decision reminder if not completed
          - pending_edits: null (cleared on success)

        Unlike finish_work (single file), this writes ONE diary entry for the
        entire batch, tagged with origin_type="diary_entry_batch".
        """
        claims_mgr = _get_claims_manager()
        if claims_mgr is None:
            return _fail(
                _phase("finish_work_batch", "init"),
                "finish_work_batch",
                reason="session coordination not available",
                hint="Run with shared_server_mode=True or transport='http'",
                failure_mode="no_coordination",
            )

        dt = _get_decision_tracker()
        if capture_decision and dt is None:
            return _fail(
                _phase("finish_work_batch", "init"),
                "finish_work_batch",
                reason="decision tracker not available",
                hint="Run with shared_server_mode=True",
                failure_mode="no_decision_tracker",
            )

        resolved = _require_session_id(ctx, session_id, "finish_work_batch")

        return _do_finish_work_batch(
            ctx, paths, resolved, diary_entry, topic, agent_name,
            capture_decision, rationale, decision_category, decision_confidence,
            claims_mgr, dt or _EmptyDecisionTracker(), backend,
        )


class _EmptyDecisionTracker:
    """Passthrough for finish_work when decision_tracker is unavailable."""
    def capture_decision(self, **kwargs):
        return {"id": None, "error": "no decision tracker"}
