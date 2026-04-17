"""
wakeup_context.py — Build compact wake-up context bundle for session resume/takeover.
"""

from __future__ import annotations

import os
from typing import Optional

from .claims_manager import ClaimsManager
from .handoff_manager import HandoffManager
from .decision_tracker import DecisionTracker
from .session_registry import SessionRegistry


def build_wakeup_context(
    session_id: str,
    project_root: Optional[str] = None,
    palace_path: Optional[str] = None,
) -> dict:
    """
    Build a compact wake-up context bundle for session resume or takeover.

    Returns a dict with:
    - active_claims: list of claims this session holds
    - pending_handoffs: handoffs addressed to this session (or broadcast)
    - recent_decisions: last 10 decisions from this session
    - recent_changes: last 20 memories from repo wing with source_file
    - conflicts: any expired claims that had conflicts
    - session_info: current session metadata from registry
    - recommended_tools: based on what's missing (handoffs need hybrid_search, etc.)
    """
    if palace_path is None:
        palace_path = os.environ.get("MEMPALACE_PATH", os.path.expanduser("~/.mempalace"))

    claims_mgr = ClaimsManager(palace_path)
    handoff_mgr = HandoffManager(palace_path)
    decision_mgr = DecisionTracker(palace_path)
    registry = SessionRegistry(palace_path)

    result = {
        "session_id": session_id,
        "project_root": project_root,
        "active_claims": [],
        "pending_handoffs": [],
        "recent_decisions": [],
        "recent_changes": [],
        "conflicts": [],
        "session_info": {},
        "recommended_tools": [],
    }

    # Active claims for this session
    try:
        result["active_claims"] = claims_mgr.get_session_claims(session_id)
    except Exception:
        pass

    # Pending handoffs for this session
    try:
        # Get directed handoffs to this session
        all_handoffs = handoff_mgr.get_handoffs_for_session(session_id)
        pending = [h for h in all_handoffs if h["status"] in ("pending", "accepted")]
        result["pending_handoffs"] = pending[:20]
    except Exception:
        pass

    # Recent decisions for this session
    try:
        decisions = decision_mgr.list_decisions(session_id=session_id, limit=10)
        result["recent_decisions"] = [
            {
                "id": d["id"],
                "category": d["category"],
                "decision_text": d["decision_text"],
                "rationale": d["rationale"],
                "confidence": d["confidence"],
                "status": d["status"],
                "created_at": d["created_at"],
            }
            for d in decisions
        ]
    except Exception:
        pass

    # Session info from registry
    try:
        session = registry.get_session(session_id)
        if session:
            result["session_info"] = {
                "session_id": session["session_id"],
                "status": session["status"],
                "branch": session.get("branch"),
                "role": session.get("role"),
                "last_seen_at": session.get("last_seen_at"),
                "claimed_paths": session.get("claimed_paths", []),
                "metadata": session.get("metadata", {}),
            }
    except Exception:
        pass

    # Conflict analysis: claims that expired while being held
    try:
        events = claims_mgr.get_recent_events(session_id=session_id, limit=100)
        conflict_events = [e for e in events if e.get("action") == "conflict"]
        for ce in conflict_events:
            result["conflicts"].append({
                "target_type": ce["target_type"],
                "target_id": ce["target_id"],
                "blocked_by": ce["payload"].get("blocked_by"),
                "timestamp": ce["timestamp"],
            })
    except Exception:
        pass

    # Recommended tools based on context
    recommended = []

    if result["pending_handoffs"]:
        recommended.append("mempalace_hybrid_search")
        recommended.append("mempalace_accept_handoff")

    if result["active_claims"]:
        recommended.append("mempalace_conflict_check")

    if not result["recent_decisions"]:
        recommended.append("mempalace_capture_decision")

    # Check for long-idle sessions
    if result["session_info"].get("status") == "idle":
        recommended.append("mempalace_wakeup_context")

    if not recommended:
        recommended = [
            "mempalace_status",
            "mempalace_search",
            "mempalace_kg_query",
        ]

    result["recommended_tools"] = recommended

    return result