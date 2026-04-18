#!/usr/bin/env python3
"""
wakeup_context.py — Build compact wake-up context bundle for session resume/takeover.

Phase 6 enrichment:
- active_symbols: symbols from recently touched files
- recent_changes: list of recently modified files
- hot_spots: files that changed most in recent commits
- relevant_decisions: decisions whose context matches current file being edited
- next_checks: suggested validation steps based on what's being edited
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

    Phase 6 returns:
    - active_claims: list of claims this session holds
    - pending_handoffs: handoffs addressed to this session (or broadcast)
    - recent_decisions: last 10 decisions from this session
    - recent_changes: last 20 memories from repo wing with source_file
    - conflicts: any expired claims that had conflicts
    - session_info: current session metadata from registry
    - recommended_tools: based on what's missing (handoffs need hybrid_search, etc.)
    - active_symbols: symbols from recently touched files
    - hot_spots: files changed most in recent commits
    - relevant_decisions: decisions related to current scope
    - next_checks: suggested validation steps
    """
    if palace_path is None:
        palace_path = os.environ.get("MEMPALACE_PATH", os.path.expanduser("~/.mempalace"))
    if project_root is None:
        project_root = os.environ.get("PROJECT_ROOT", "")

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
        # Phase 6 additions
        "active_symbols": [],
        "hot_spots": [],
        "relevant_decisions": [],
        "next_checks": [],
    }

    # Active claims for this session
    try:
        result["active_claims"] = claims_mgr.get_session_claims(session_id)
    except Exception:
        pass

    # Pending handoffs for this session
    try:
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

    # Phase 6: Recent changes from git
    if project_root:
        try:
            from .recent_changes import get_recent_changes, get_hot_spots
            result["recent_changes"] = get_recent_changes(project_root, n=20)
            result["hot_spots"] = get_hot_spots(project_root, n=5)
        except Exception:
            pass

    # Phase 6: Symbols from active scope (files from active claims)
    try:
        if result["active_claims"] and palace_path:
            from .symbol_index import SymbolIndex
            si = SymbolIndex.get(palace_path)
            seen_files = set()
            symbols = []
            for claim in result["active_claims"]:
                target = claim.get("target_id", "")
                if target in seen_files:
                    continue
                seen_files.add(target)
                file_syms = si.get_file_symbols(target)
                for sym in file_syms.get("symbols", [])[:5]:  # limit to 5 per file
                    symbols.append({
                        "name": sym["name"],
                        "type": sym.get("type", "definition"),
                        "file_path": target,
                        "line_start": sym.get("line_start", 0),
                    })
            result["active_symbols"] = symbols[:20]  # cap at 20 total
    except Exception:
        pass

    # Phase 6: Relevant decisions (decisions whose rationale mentions active files)
    try:
        if result["active_claims"] and result["recent_decisions"]:
            active_files = set(c.get("target_id", "") for c in result["active_claims"])
            relevant = []
            for d in result["recent_decisions"]:
                rationale = d.get("rationale", "") or ""
                # Simple check: any active file mentioned in rationale
                for af in active_files:
                    if af in rationale:
                        relevant.append(d)
                        break
            result["relevant_decisions"] = relevant[:5]
    except Exception:
        pass

    # Phase 6: Next checks based on context
    next_checks = []
    try:
        if result["pending_handoffs"]:
            next_checks.append({
                "check": "Review pending handoffs before starting new work",
                "tool": "mempalace_pull_handoffs",
                "priority": "high",
            })
        if result["conflicts"]:
            next_checks.append({
                "check": f"Resolve {len(result['conflicts'])} claim conflict(s)",
                "tool": "mempalace_conflict_check",
                "priority": "high",
            })
        if result["hot_spots"]:
            top_spot = result["hot_spots"][0]
            next_checks.append({
                "check": f"Hot file changed recently: {top_spot['file_path']} ({top_spot.get('change_count', '?')} changes)",
                "tool": "mempalace_file_symbols",
                "priority": "medium",
            })
        if result["active_symbols"]:
            next_checks.append({
                "check": f"Validate symbols in {len(result['active_symbols'])} active file(s)",
                "tool": "mempalace_search",
                "priority": "low",
            })
    except Exception:
        pass

    result["next_checks"] = next_checks

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

    if result["recent_changes"]:
        recommended.append("mempalace_recent_changes")

    if not recommended:
        recommended = [
            "mempalace_status",
            "mempalace_search",
            "mempalace_kg_query",
        ]

    result["recommended_tools"] = recommended

    return result