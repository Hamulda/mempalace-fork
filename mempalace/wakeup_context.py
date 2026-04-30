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

import json
import os
import urllib.request
from typing import Optional

from .claims_manager import ClaimsManager
from .handoff_manager import HandoffManager
from .decision_tracker import DecisionTracker
from .session_registry import SessionRegistry
from .server._project_root import _find_git_root


def build_wakeup_context(
    session_id: str,
    project_root: Optional[str] = None,
    palace_path: Optional[str] = None,
) -> dict:
    """
    Build a compact wake-up context bundle for session resume or takeover.

    Returns:
    - session_id, project_root: identifying info
    - active_claims: unexpired claims this session holds
    - pending_handoffs: directed handoffs (from/to this session) AND broadcast
      handoffs (to_session_id=None), limited to 20, filtered to pending/accepted
    - recent_decisions: last 10 decisions created by this session (active only,
      not expired or superseded)
    - recent_changes: last 20 memories from repo wing with source_file
    - conflicts: expired claims that had conflicts (from claim_events)
    - session_info: current session metadata from registry
    - recommended_tools: based on context gaps
    - active_symbols: symbols from recently touched files (Phase 6)
    - hot_spots: files changed most in recent commits (Phase 6)
    - relevant_decisions: decisions whose rationale mentions active file paths (Phase 6)
    - next_checks: suggested validation steps (Phase 6)
    """
    if palace_path is None:
        palace_path = os.environ.get("MEMPALACE_PATH", os.path.expanduser("~/.mempalace"))
    if project_root is None:
        project_root = _find_git_root(palace_path) or ""

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

    # Pending handoffs for this session (directed + broadcast)
    # Directed: from_session_id=X OR to_session_id=X
    # Broadcast: to_session_id IS NULL (available to any session)
    try:
        directed = handoff_mgr.get_handoffs_for_session(session_id)
        broadcasts = handoff_mgr.pull_handoffs(session_id=None, status="pending")
        seen = set()
        merged = []
        for h in directed + broadcasts:
            if h["id"] not in seen and h["status"] in ("pending", "accepted"):
                seen.add(h["id"])
                merged.append(h)
        result["pending_handoffs"] = merged[:20]
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
            # Normalize: add canonical path fields (source_file=absolute, repo_rel_path=git-relative)
            _pr = os.path.abspath(project_root)
            for entry in result["recent_changes"]:
                entry["abs_path"] = os.path.normpath(os.path.join(_pr, entry["file_path"]))
                entry["source_file"] = entry["abs_path"]  # canonical identity (absolute)
                entry["repo_rel_path"] = entry["file_path"]  # user-friendly (git-relative)
            for entry in result["hot_spots"]:
                entry["abs_path"] = os.path.normpath(os.path.join(_pr, entry["file_path"]))
                entry["source_file"] = entry["abs_path"]  # canonical identity (absolute)
                entry["repo_rel_path"] = entry["file_path"]  # user-friendly (git-relative)
        except Exception:
            pass

    # Phase 6: Symbols from active scope (files from active claims)
    # Enriched with caller context for better Claude Code workflow
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
                    sym_entry = {
                        "name": sym["name"],
                        "type": sym.get("type", "definition"),
                        "file_path": target,
                        "line_start": sym.get("line_start", 0),
                    }
                    # Add caller context (files that import this symbol's module)
                    if project_root:
                        callers = si.get_callers(sym["name"], project_root)
                        if callers:
                            sym_entry["callers"] = [
                                {
                                    "file_path": c["file_path"],
                                    "import_type": c.get("import_type", "module"),
                                }
                                for c in callers[:3]  # max 3 callers per symbol
                            ]
                    symbols.append(sym_entry)
            result["active_symbols"] = symbols[:20]  # cap at 20 total
    except Exception:
        pass

    # Phase 6: Relevant decisions from this session whose rationale mentions active files
    # Note: only checks this session's decisions (recent_decisions), not all-palace decisions
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


def _path_boundary_contains(child_path: str, parent_path: str) -> bool:
    """
    Returns True if child_path is strictly under parent_path.

    /proj matches /proj, /proj/foo, /proj/foo/bar
    /proj-old does NOT match /proj (different prefix after common prefix)
    """
    if not child_path or not parent_path:
        return False
    cp = child_path.rstrip("/")
    pp = parent_path.rstrip("/")
    if cp == pp:
        return True
    # Ensure parent is a directory boundary — require leading slash or exact match
    # /proj  matches /proj/foo
    # /proj  does NOT match /proj-old
    return cp.startswith(pp + "/")


def _probe_embed_daemon_socket() -> tuple[str, str, int] | None:
    """
    Lightweight Unix socket probe of embed daemon — no model loading.

    Returns (provider, model_id, dims) or None if daemon unavailable.
    """
    try:
        from mempalace.embed_metadata import _probe_daemon_socket
        result = _probe_daemon_socket()
        if result is not None:
            return result
    except Exception:
        pass
    return None


def _load_stored_embedding_meta(palace_path: str) -> dict:
    """Load stored embedding metadata from palace, or empty dict."""
    try:
        from mempalace.embed_metadata import load_meta
        meta = load_meta(palace_path)
        if meta:
            return meta
    except Exception:
        pass
    return {}


def _get_index_counts(palace_path: str) -> dict:
    """
    Return cheap index statistics: path_index_count, fts5_count, symbol_count.
    Each count is O(1) or fast single-statement SQL — no scanning.
    """
    counts: dict[str, int | None] = {"path_index_count": None, "fts5_count": None, "symbol_count": None}
    try:
        from mempalace.path_index import PathIndex
        pi = PathIndex.get(palace_path)
        counts["path_index_count"] = pi.count()
    except Exception:
        pass
    try:
        from mempalace.lexical_index import KeywordIndex
        li = KeywordIndex.get(palace_path)
        counts["fts5_count"] = li.count()
    except Exception:
        pass
    try:
        from mempalace.symbol_index import SymbolIndex
        si = SymbolIndex.get(palace_path)
        counts["symbol_count"] = si.stats()["total_symbols"]
    except Exception:
        pass
    return counts


def build_startup_context(
    session_id: str,
    project_path: str | None = None,
    palace_path: str | None = None,
    limit: int = 8,
) -> dict:
    """
    Build compact startup context for a Claude Code session.

    Inputs:
    - project_path: optional project root to scope claims/handoffs
    - session_id: required, auto-detected by the MCP tool wrapper
    - palace_path: palace data directory
    - limit: max handoffs to return (default 8)

    Returns:
    - server_health: HTTP health ping result
    - palace_path: palace data path
    - backend: storage backend (always 'lance')
    - python_version: sys.version_info string
    - embedding_stored_provider: provider from embedding_meta.json (or null)
    - embedding_stored_model_id: model_id from embedding_meta.json (or null)
    - embedding_stored_dims: dims from embedding_meta.json (or null)
    - embedding_current_provider: provider from live daemon socket probe (or null)
    - embedding_current_model_id: model_id from live probe (or null)
    - embedding_current_dims: dims from live probe (or null)
    - embedding_drift_detected: true/false/unknown
    - embedding_provider: legacy field — current provider or stored or unknown
    - embedding_meta: legacy field — {model_id, embed_batch_size} if available
    - active_sessions: count from session registry
    - current_claims: claims for project_path (or all if no project_path)
    - pending_handoffs: handoffs for this session (limited)
    - recommended_first_actions: startup workflow steps
    - project_path_reminder: the project_path passed in or derived
    - m1_defaults: bounded defaults for M1/8GB runs
    - path_index_count: rows in path_index.sqlite3 (or null)
    - fts5_count: rows in FTS5 table (or null)
    - symbol_count: top-level definitions in symbol index (or null)
    """
    import sys
    from .session_registry import SessionRegistry

    if palace_path is None:
        palace_path = os.environ.get(
            "MEMPALACE_PATH", os.path.expanduser("~/.mempalace/palace")
        )

    # Resolve project_path — use as-is if provided, derive from git root otherwise
    resolved_project = project_path
    if resolved_project is None:
        resolved_project = _find_git_root(palace_path) or ""

    claims_mgr = ClaimsManager(palace_path)
    handoff_mgr = HandoffManager(palace_path)

    # ── Server health via HTTP probe ────────────────────────────────────────
    server_health = {"status": "unknown", "pid": None, "transport": None, "url": None}
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8765/health",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = resp.read()
            server_health = {"status": "ok", "health": json.loads(data)}
    except Exception:
        pass

    # ── Embedding state: stored metadata ───────────────────────────────────
    stored = _load_stored_embedding_meta(palace_path)
    embedding_stored_provider = stored.get("provider")
    embedding_stored_model_id = stored.get("model_id")
    embedding_stored_dims = stored.get("dims")

    # ── Embedding state: current via Unix socket probe ───────────────────
    current = _probe_embed_daemon_socket()
    if current is not None:
        embedding_current_provider, embedding_current_model_id, embedding_current_dims = current
    else:
        embedding_current_provider = None
        embedding_current_model_id = None
        embedding_current_dims = None

    # ── Drift detection ────────────────────────────────────────────────────
    if embedding_current_provider is not None and embedding_stored_provider is not None:
        embedding_drift_detected = (
            embedding_current_provider != embedding_stored_provider
            or embedding_current_model_id != embedding_stored_model_id
        )
    elif embedding_current_provider is not None:
        embedding_drift_detected = "unknown"  # have current, no stored
    elif embedding_stored_provider is not None:
        embedding_drift_detected = "unknown"  # have stored, no current (daemon down)
    else:
        embedding_drift_detected = "unknown"  # neither available

    # ── Legacy compat fields ───────────────────────────────────────────────
    # embedding_provider: prefer current > stored > unknown
    if embedding_current_provider:
        embedding_provider = embedding_current_provider
    elif embedding_stored_provider:
        embedding_provider = embedding_stored_provider
    else:
        embedding_provider = "unknown"

    embedding_meta = {}
    if embedding_current_model_id:
        embedding_meta["model_id"] = embedding_current_model_id
    elif embedding_stored_model_id:
        embedding_meta["model_id"] = embedding_stored_model_id

    # ── Active sessions ──────────────────────────────────────────────────────
    active_sessions = 0
    try:
        reg = SessionRegistry(palace_path)
        sessions = reg.get_active_sessions(project_root=resolved_project or None)
        active_sessions = len(sessions)
    except Exception:
        pass

    # ── Claims for project_path ────────────────────────────────────────────
    current_claims = []
    try:
        all_claims = claims_mgr.list_active_claims()
        if resolved_project:
            all_claims = [
                c for c in all_claims
                if _path_boundary_contains(c.get("target_id", ""), resolved_project)
            ]
        current_claims = [
            {
                "path": c["target_id"],
                "owner": c.get("owner"),
                "expires_at": c.get("expires_at"),
            }
            for c in all_claims[:50]
        ]
    except Exception:
        pass

    # ── Pending handoffs for session ───────────────────────────────────────
    pending_handoffs = []
    try:
        directed = handoff_mgr.get_handoffs_for_session(session_id)
        broadcasts = handoff_mgr.pull_handoffs(session_id=None, status="pending")
        seen = set()
        merged = []
        for h in directed + broadcasts:
            if h["id"] not in seen and h["status"] in ("pending", "accepted"):
                seen.add(h["id"])
                merged.append(h)
        pending_handoffs = merged[:limit]
    except Exception:
        pass

    # ── Recommended first actions ────────────────────────────────────────────
    recommended = []
    if current_claims:
        recommended.append({
            "action": "mempalace_list_claims",
            "reason": f"{len(current_claims)} active claim(s) in workspace",
            "priority": "medium",
        })
    if pending_handoffs:
        recommended.append({
            "action": "mempalace_pull_handoffs",
            "reason": f"{len(pending_handoffs)} pending handoff(s) for this session",
            "priority": "high",
        })
    if not current_claims and not pending_handoffs:
        recommended.append({
            "action": "mempalace_status",
            "reason": "No active claims or handoffs — check palace overview first",
            "priority": "high",
        })
    recommended.extend([
        {
            "action": "mempalace_search",
            "reason": "Verify facts before responding — never guess",
            "priority": "medium",
        },
        {
            "action": "mempalace_diary_write",
            "reason": "Record what happened at end of session",
            "priority": "low",
        },
    ])

    # ── M1 defaults ─────────────────────────────────────────────────────────
    m1_defaults = {
        "max_batch": 32,
        "embed_batch_default": 64,
        "memory_guard_active": True,
        "query_cache_ttl": 300,
        "claim_timeout_seconds": 60,
        "session_timeout_seconds": 300,
    }

    # ── Index counts ─────────────────────────────────────────────────────────
    index_counts = _get_index_counts(palace_path)

    return {
        "server_health": server_health,
        "palace_path": palace_path,
        "backend": "lance",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        # New truthful embedding state
        "embedding_stored_provider": embedding_stored_provider,
        "embedding_stored_model_id": embedding_stored_model_id,
        "embedding_stored_dims": embedding_stored_dims,
        "embedding_current_provider": embedding_current_provider,
        "embedding_current_model_id": embedding_current_model_id,
        "embedding_current_dims": embedding_current_dims,
        "embedding_drift_detected": embedding_drift_detected,
        # Legacy compat
        "embedding_provider": embedding_provider,
        "embedding_meta": embedding_meta,
        "active_sessions": active_sessions,
        "current_claims": current_claims,
        "current_claims_count": len(current_claims),
        "pending_handoffs": pending_handoffs,
        "pending_handoffs_count": len(pending_handoffs),
        "recommended_first_actions": recommended,
        "project_path_reminder": resolved_project,
        "m1_defaults": m1_defaults,
        # Index stats
        "path_index_count": index_counts["path_index_count"],
        "fts5_count": index_counts["fts5_count"],
        "symbol_count": index_counts["symbol_count"],
    }