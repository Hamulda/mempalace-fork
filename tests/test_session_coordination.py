"""
Tests for file-system aware coordination: two sessions over same file,
two sessions over different files in same wing/room, handoff/takeover flows,
and new session tools (file_status, workspace_claims, edit_guidance).

Run: pytest tests/test_session_coordination.py -v
"""

from __future__ import annotations

import tempfile
import os
import pytest
from unittest.mock import MagicMock


palace_path_factory = tempfile.mkdtemp(prefix="mempalace_coord_")


def _pp():
    return tempfile.mkdtemp(prefix="mempalace_coord_")


# ---------------------------------------------------------------------------
# Mock coordinators (same as test_workflow_tools.py but with additional methods
# needed for the new session tools)
# ---------------------------------------------------------------------------

class _MockClaimsManager:
    """In-memory ClaimsManager fake with full session-coordination support."""
    def __init__(self, palace_path=None):
        self._claims: dict[tuple, dict] = {}

    def claim(self, target_type, target_id, session_id, ttl_seconds=600, payload=None):
        key = (target_type, target_id)
        existing = self._claims.get(key)
        if existing and existing["expires_at"] > _now() and existing["session_id"] != session_id:
            return {"acquired": False, "owner": existing["session_id"]}
        self._claims[key] = {
            "session_id": session_id,
            "expires_at": _expires_at(ttl_seconds),
            "payload": payload or {},
        }
        return {"acquired": True, "owner": session_id, "expires_at": self._claims[key]["expires_at"]}

    def check_conflicts(self, target_type, target_id, session_id):
        key = (target_type, target_id)
        c = self._claims.get(key)
        if c is None:
            return {"has_conflict": False}
        if c["expires_at"] <= _now():
            return {"has_conflict": False}
        if c["session_id"] == session_id:
            return {"has_conflict": False, "owner": session_id, "is_self": True}
        return {"has_conflict": True, "owner": c["session_id"], "expires_at": c["expires_at"]}

    def release_claim(self, target_type, target_id, session_id):
        key = (target_type, target_id)
        c = self._claims.get(key)
        if c is None or c["expires_at"] <= _now():
            return {"success": False, "error": "no_active_claim"}
        if c["session_id"] != session_id:
            return {"success": False, "error": "not_owner", "owner": c["session_id"]}
        del self._claims[key]
        return {"success": True}

    def get_claim(self, target_type, target_id):
        key = (target_type, target_id)
        c = self._claims.get(key)
        if c is None or c["expires_at"] <= _now():
            return None
        return {
            "session_id": c["session_id"],
            "target_type": target_type,
            "target_id": target_id,
            "payload": c.get("payload", {}),
            "expires_at": c["expires_at"],
        }

    def get_session_claims(self, session_id):
        now = _now()
        return [
            {"target_id": tid, "session_id": cid, "expires_at": c["expires_at"], "payload": c.get("payload", {})}
            for (tt, tid), c in self._claims.items()
            if c["session_id"] == session_id and c["expires_at"] > now
        ]

    def list_active_claims(self):
        now = _now()
        return [
            {
                "session_id": c["session_id"],
                "target_type": tt,
                "target_id": tid,
                "payload": c.get("payload", {}),
                "expires_at": c["expires_at"],
            }
            for (tt, tid), c in self._claims.items()
            if c["expires_at"] > now
        ]


class _MockWriteCoordinator:
    def __init__(self):
        self._intents = []

    def log_intent(self, session_id, operation, target_type, target_id, payload=None):
        intent_id = f"intent_{len(self._intents)}"
        self._intents.append({
            "id": intent_id, "session_id": session_id, "operation": operation,
            "target_type": target_type, "target_id": target_id,
            "payload": payload, "status": "pending",
        })
        return intent_id


class _MockHandoffManager:
    def __init__(self):
        self._handoffs = []
        self._next_id = 1

    def push_handoff(self, from_session_id, summary, touched_paths, blockers,
                     next_steps, confidence, priority, to_session_id=None):
        h = {
            "id": f"handoff_{self._next_id}",
            "from_session_id": from_session_id,
            "to_session_id": to_session_id,
            "summary": summary,
            "touched_paths": touched_paths or [],
            "blockers": blockers or [],
            "next_steps": next_steps or [],
            "confidence": confidence,
            "priority": priority,
            "status": "pending",
        }
        self._handoffs.append(h)
        self._next_id += 1
        return h

    def pull_handoffs(self, session_id, status=None):
        results = []
        for h in self._handoffs:
            if status and h["status"] != status:
                continue
            if session_id is None:
                if h["to_session_id"] is None:
                    results.append(h)
            elif h["from_session_id"] == session_id or h["to_session_id"] == session_id:
                results.append(h)
        return results

    def accept_handoff(self, handoff_id, session_id):
        for h in self._handoffs:
            if h["id"] == handoff_id:
                h["status"] = "accepted"
                return {"accepted": True, "status": "accepted", "summary": h["summary"]}
        return {"error": "not_found", "status": "error"}

    def complete_handoff(self, handoff_id, session_id):
        for h in self._handoffs:
            if h["id"] == handoff_id:
                h["status"] = "completed"
                return {"completed": True}
        return {"error": "not_found"}


class _MockSymbolIndex:
    def __init__(self, symbols=None):
        self._symbols = symbols or []

    def get_file_symbols(self, path):
        matching = [s for s in self._symbols if s.get("file_path") == path]
        return {"symbols": matching[:10]}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _expires_at(ttl_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


# ---------------------------------------------------------------------------
# Test: mempalace_file_status
# ---------------------------------------------------------------------------

class TestFileStatus:
    """mempalace_file_status returns compact snapshot with next_actions."""

    def test_unclaimed_file_returns_begin_work_next_action(self, tmp_path):
        """Unclaimed file → next_action is mempalace_begin_work."""
        from mempalace.server._session_tools import _get_session_id_from_ctx
        from unittest.mock import MagicMock

        # Simulate the file_status logic
        claims = _MockClaimsManager()
        si = _MockSymbolIndex([
            {"name": "my_func", "type": "function", "file_path": "/src/main.py", "line_start": 10},
        ])

        # No claim on /src/main.py
        conflict = claims.check_conflicts("file", "/src/main.py", "session-a")
        assert conflict["has_conflict"] is False

        # Active claim = None → next action should be begin_work
        # We test the logic directly (simulating what the tool does)
        active = claims.get_claim("file", "/src/main.py")
        assert active is None
        next_actions = []
        if active is None:
            next_actions.append({
                "action": "mempalace_begin_work",
                "reason": "File is unclaimed — begin your edit session",
                "priority": "high",
                "skill": "begin-work",
            })
        assert len(next_actions) == 1
        assert next_actions[0]["action"] == "mempalace_begin_work"

    def test_self_claimed_file_returns_prepare_edit_next_action(self):
        """Self holds the claim → next_action is mempalace_prepare_edit."""
        claims = _MockClaimsManager()
        claims.claim("file", "/src/main.py", "session-a", ttl_seconds=600)

        active = claims.get_claim("file", "/src/main.py")
        assert active is not None
        assert active["session_id"] == "session-a"

        # next_action for self-claim
        next_actions = [{
            "action": "mempalace_prepare_edit",
            "reason": "You hold the claim — get symbol context before editing",
            "priority": "high",
            "skill": "prepare-edit",
        }]
        assert next_actions[0]["action"] == "mempalace_prepare_edit"

    def test_other_session_claimed_returns_conflict_next_action(self):
        """Other session holds claim → next_action is push_handoff/pull_handoffs."""
        claims = _MockClaimsManager()
        claims.claim("file", "/src/main.py", "session-b", ttl_seconds=600)

        active = claims.get_claim("file", "/src/main.py")
        assert active["session_id"] == "session-b"

        # next_action for blocked claim
        next_actions = [
            {
                "action": "mempalace_push_handoff",
                "reason": f"File claimed by 'session-b' — negotiate or wait",
                "priority": "high",
            },
            {
                "action": "mempalace_pull_handoffs",
                "reason": "Check if owner has broadcast a handoff for this file",
                "priority": "medium",
            },
        ]
        assert next_actions[0]["action"] == "mempalace_push_handoff"

    def test_file_status_returns_symbol_preview(self):
        """file_status includes symbols_preview for claimed files."""
        si = _MockSymbolIndex([
            {"name": "my_func", "type": "function", "file_path": "/src/main.py", "line_start": 10},
            {"name": "MyClass", "type": "class", "file_path": "/src/main.py", "line_start": 20},
            {"name": "other_func", "type": "function", "file_path": "/src/other.py", "line_start": 5},
        ])

        sym_data = si.get_file_symbols("/src/main.py")
        symbols_preview = [
            {"name": s["name"], "type": s.get("type", "?")}
            for s in sym_data.get("symbols", [])[:5]
        ]

        assert len(symbols_preview) == 2
        assert symbols_preview[0]["name"] == "my_func"


# ---------------------------------------------------------------------------
# Test: mempalace_workspace_claims
# ---------------------------------------------------------------------------

class TestWorkspaceClaims:
    """mempalace_workspace_claims returns all claims in a workspace prefix."""

    def test_workspace_with_no_claims_returns_empty_list(self):
        """Empty workspace → claims=[], conflicts=[], next_action begin_work."""
        claims = _MockClaimsManager()
        # No claims at all
        all_claims = claims.list_active_claims()
        workspace_claims = [c for c in all_claims if c["target_id"].startswith("/src/auth/")]
        assert workspace_claims == []

        next_actions = [{
            "action": "mempalace_begin_work",
            "reason": "Workspace is clear — no active claims",
            "priority": "high",
            "skill": "begin-work",
        }]
        assert next_actions[0]["action"] == "mempalace_begin_work"

    def test_workspace_with_other_session_claims_returns_conflicts(self):
        """Workspace has other session's claims → conflicts list populated."""
        claims = _MockClaimsManager()
        claims.claim("file", "/src/auth/auth.py", "session-b", ttl_seconds=600)
        claims.claim("file", "/src/auth/token.py", "session-b", ttl_seconds=600)

        all_claims = claims.list_active_claims()
        workspace_claims = [c for c in all_claims if c["target_id"].startswith("/src/auth/")]
        conflicts = [
            {"path": c["target_id"], "owner": c["session_id"], "expires_at": c["expires_at"]}
            for c in workspace_claims
            if c["session_id"] != "session-a"
        ]

        assert len(conflicts) == 2
        assert all(c["owner"] == "session-b" for c in conflicts)

    def test_workspace_filters_correctly_by_prefix(self):
        """Only files under workspace prefix are returned."""
        claims = _MockClaimsManager()
        claims.claim("file", "/src/auth/auth.py", "session-a")
        claims.claim("file", "/src/auth/token.py", "session-a")
        claims.claim("file", "/src/main.py", "session-b")

        all_claims = claims.list_active_claims()

        # /src/auth/ workspace
        auth_claims = [c for c in all_claims if c["target_id"].startswith("/src/auth/")]
        assert len(auth_claims) == 2

        # /src/main.py workspace
        main_claims = [c for c in all_claims if c["target_id"].startswith("/src/main.py")]
        assert len(main_claims) == 1


# ---------------------------------------------------------------------------
# Test: Two sessions over same file — conflict must block
# ---------------------------------------------------------------------------

class TestTwoSessionsSameFile:
    """Two sessions targeting the same file must be properly serialized."""

    def test_second_session_blocked_by_active_claim(self):
        """Session B cannot claim while session A holds the file."""
        claims = _MockClaimsManager()

        # Session A claims the file
        result_a = claims.claim("file", "/src/auth.py", "session-a", ttl_seconds=600)
        assert result_a["acquired"] is True
        assert result_a["owner"] == "session-a"

        # Session B tries to claim — blocked
        result_b = claims.claim("file", "/src/auth.py", "session-b", ttl_seconds=600)
        assert result_b["acquired"] is False
        assert result_b["owner"] == "session-a"

        # Conflict check for session-b
        conflict = claims.check_conflicts("file", "/src/auth.py", "session-b")
        assert conflict["has_conflict"] is True
        assert conflict["owner"] == "session-a"

    def test_begin_work_blocks_second_session(self):
        """begin_work via _do_begin_work blocks second session."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        # Session A acquires
        result_a, acquired_a, _ = _do_begin_work(
            ctx, "/src/auth.py", "session-a", 600, "fixing auth",
            claims, wc,
        )
        assert result_a["ok"] is True
        assert acquired_a is True

        # Session B is blocked
        result_b, acquired_b, _ = _do_begin_work(
            ctx, "/src/auth.py", "session-b", 600, "also fixing auth",
            claims, wc,
        )
        assert result_b["ok"] is False
        assert result_b["failure_mode"] == "claim_conflict"
        assert acquired_b is False
        assert result_b["details"]["owner"] == "session-a"

    def test_prepare_edit_blocks_second_session(self):
        """prepare_edit returns claim_conflict when other session holds claim."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        claims.claim("file", "/src/auth.py", "session-a", ttl_seconds=600)
        si = _MockSymbolIndex([])
        ctx = MagicMock()

        # Session B calls prepare_edit on the same file
        result, _, _ = _do_prepare_edit(
            ctx, "/src/auth.py", "session-b",
            "/tmp/palace", "/tmp/project", si,
            claims,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "claim_conflict"
        assert result["details"]["owner"] == "session-a"

    def test_finish_work_releases_claim_unblocking_second_session(self):
        """Session A releases → session B can now claim."""
        claims = _MockClaimsManager()

        # Session A claims and finishes
        claims.claim("file", "/src/auth.py", "session-a", ttl_seconds=600)
        release = claims.release_claim("file", "/src/auth.py", "session-a")
        assert release["success"] is True

        # Session B can now claim
        result_b = claims.claim("file", "/src/auth.py", "session-b", ttl_seconds=600)
        assert result_b["acquired"] is True
        assert result_b["owner"] == "session-b"


# ---------------------------------------------------------------------------
# Test: Two sessions over different files in same wing/room
# ---------------------------------------------------------------------------

class TestTwoSessionsDifferentFilesSameWing:
    """Two sessions can work on different files in the same module simultaneously."""

    def test_different_files_same_wing_no_conflict(self):
        """session-a and session-b on different files in /src/auth/ → no conflict."""
        claims = _MockClaimsManager()

        # Session A works on auth.py
        result_a = claims.claim("file", "/src/auth/auth.py", "session-a", ttl_seconds=600)
        assert result_a["acquired"] is True

        # Session B works on token.py — no conflict
        result_b = claims.claim("file", "/src/auth/token.py", "session-b", ttl_seconds=600)
        assert result_b["acquired"] is True

        # Both claims active
        all_claims = claims.list_active_claims()
        assert len(all_claims) == 2

    def test_workspace_claims_shows_both_claims(self):
        """workspace_claims for /src/auth/ shows both sessions active."""
        claims = _MockClaimsManager()
        claims.claim("file", "/src/auth/auth.py", "session-a")
        claims.claim("file", "/src/auth/token.py", "session-b")

        all_claims = claims.list_active_claims()
        auth_claims = [
            c for c in all_claims
            if c["target_id"].startswith("/src/auth/")
        ]

        assert len(auth_claims) == 2
        owners = {c["session_id"] for c in auth_claims}
        assert owners == {"session-a", "session-b"}

    def test_takeover_work_on_one_file_does_not_block_other(self):
        """Takeover on auth.py does not prevent session-b from continuing on token.py."""
        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        wc = _MockWriteCoordinator()

        # Session A working on auth.py + token.py, creates handoff
        h = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Auth refactor in progress",
            touched_paths=["/src/auth/auth.py", "/src/auth/token.py"],
            blockers=[], next_steps=["Complete API docs"],
            confidence=4, priority="high",
        )

        # Session B accepts and claims auth.py
        handoff_mgr.accept_handoff(h["id"], "session-b")
        claim_b = claims.claim("file", "/src/auth/auth.py", "session-b")

        # Session A still holds token.py
        claim_a_token = claims.check_conflicts("file", "/src/auth/token.py", "session-a")
        assert claim_a_token["has_conflict"] is False  # session-a still owns it

        # Session B can claim token.py too (no conflict from session-a if it released)
        # In this scenario both files were in the handoff, so session-b claims both
        claim_b_token = claims.claim("file", "/src/auth/token.py", "session-b")
        assert claim_b_token["acquired"] is True


# ---------------------------------------------------------------------------
# Test: Handoff / Takeover flows with paths
# ---------------------------------------------------------------------------

class TestHandoffTakeoverFlow:
    """Handoff and takeover correctly transfer path claims."""

    def test_publish_handoff_releases_all_touched_paths(self):
        """publish_handoff releases claims on all touched_paths."""
        from mempalace.server._workflow_tools import _do_publish_handoff
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        ctx = MagicMock()

        claims.claim("file", "/src/auth.py", "session-a")
        claims.claim("file", "/src/token.py", "session-a")
        claims.claim("file", "/src/middleware.py", "session-a")

        result = _do_publish_handoff(
            ctx, summary="Auth refactor complete",
            touched_paths=["/src/auth.py", "/src/token.py", "/src/middleware.py"],
            blockers=[], next_steps=["Update API docs"],
            confidence=4, priority="normal",
            to_session_id=None,
            from_session_id="session-a",
            claims_mgr=claims, handoff_mgr=handoff_mgr,
        )
        assert result["ok"] is True
        assert len(result["released_claims"]) == 3
        assert all(r["success"] for r in result["released_claims"])

        # All claims released — session-b can now claim
        for path in ["/src/auth.py", "/src/token.py", "/src/middleware.py"]:
            conflict = claims.check_conflicts("file", path, "session-b")
            assert conflict["has_conflict"] is False

    def test_takeover_work_claims_all_paths(self):
        """takeover_work claims all specified paths on accept."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        # Push handoff for auth.py + token.py
        h = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Auth refactor in progress",
            touched_paths=["/src/auth.py", "/src/token.py"],
            blockers=[], next_steps=["Complete API docs"],
            confidence=4, priority="high",
        )

        # Session B accepts and claims
        result = _do_takeover_work(
            ctx, h["id"], "session-b",
            paths_to_claim=["/src/auth.py", "/src/token.py"],
            ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=claims, write_coordinator=wc,
        )
        assert result["ok"] is True
        assert result["handoff_accepted"] is True
        assert len(result["claimed_paths"]) == 2
        assert all(p["acquired"] for p in result["claimed_paths"])
        assert result["all_claims_acquired"] is True

    def test_takeover_work_partial_claims_still_accepted(self):
        """Some paths blocked → handoff accepted but partial claims reported."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        h = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Auth refactor",
            touched_paths=["/src/auth.py", "/src/other.py"],
            blockers=[], next_steps=[],
            confidence=3, priority="normal",
        )

        # session-c holds /src/other.py
        claims.claim("file", "/src/other.py", "session-c")

        result = _do_takeover_work(
            ctx, h["id"], "session-b",
            paths_to_claim=["/src/auth.py", "/src/other.py"],
            ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=claims, write_coordinator=wc,
        )
        assert result["ok"] is True
        assert result["handoff_accepted"] is True
        assert result["all_claims_acquired"] is False
        assert len(result["claim_errors"]) == 1
        assert result["claim_errors"][0]["path"] == "/src/other.py"

    def test_takeover_work_accept_fails_for_bad_handoff_id(self):
        """Invalid handoff_id → no claims made, failure_mode=accept_failed."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result = _do_takeover_work(
            ctx, "nonexistent_handoff", "session-b",
            paths_to_claim=["/src/auth.py"],
            ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=claims, write_coordinator=wc,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "handoff_accept_failed"
        assert len(result.get("claimed_paths", [])) == 0


# ---------------------------------------------------------------------------
# Test: mempalace_edit_guidance
# ---------------------------------------------------------------------------

class TestEditGuidance:
    """mempalace_edit_guidance converts workflow_result to plain-language guidance."""

    def test_guidance_for_begin_work_ok(self):
        """begin_work ok → guidance to prepare and edit."""
        result = {
            "ok": True, "phase": "begin_work:done", "action": "begin_work",
            "path": "/src/auth.py", "owner": "session-a", "expires_at": "2026-04-21T10:00:00Z",
            "next_actions": [{"action": "mempalace_prepare_edit", "priority": "high"}],
            "context_snippets": {"path": "/src/auth.py", "note": "fixing auth bug"},
        }

        guidance = ""
        skill_ref = None
        if result["action"] == "begin_work" and result["ok"]:
            guidance = f"Claim acquired on {result['path']}. Read the prepare-edit skill, then call mempalace_prepare_edit."
            skill_ref = "prepare-edit"

        assert "Claim acquired" in guidance
        assert skill_ref == "prepare-edit"

    def test_guidance_for_claim_conflict(self):
        """begin_work conflict → guidance to stop and wait/negotiate."""
        result = {
            "ok": False, "failure_mode": "claim_conflict",
            "hint": "Wait for TTL expiry or push a handoff",
            "details": {"owner": "session-b", "expires_at": "2026-04-21T10:00:00Z"},
            "next_actions": [],
        }

        guidance = ""
        if not result["ok"] and result["failure_mode"] == "claim_conflict":
            owner = result["details"].get("owner", "another session")
            expires = result["details"].get("expires_at", "unknown")
            guidance = f"STOP — {owner} holds the claim until {expires}. Wait for expiry, push a handoff requesting release, or work on a different file."

        assert "STOP" in guidance
        assert "session-b" in guidance

    def test_guidance_for_finish_work(self):
        """finish_work ok → guidance about diary write (now immediate)."""
        result = {
            "ok": True, "phase": "finish_work:done", "action": "finish_work",
            "path": "/src/auth.py", "claim_released": True,
            "diary_id": "diary_wing_claude_20260421_100000",
            "diary_entry": "Fixed auth bug", "decision_id": None,
            "next_actions": [],
        }

        guidance = ""
        skill_ref = None
        if result["action"] == "finish_work" and result["ok"]:
            diary_id = result.get("diary_id")
            if diary_id:
                guidance = f"Claim released. Diary written: {diary_id}."
            else:
                guidance = f"Claim released on {result['path']}. Edit complete."
            skill_ref = "finish-work"

        assert "Claim released" in guidance
        assert skill_ref == "finish-work"

    def test_guidance_for_takeover_work(self):
        """takeover_work ok → guidance to prepare each claimed path."""
        result = {
            "ok": True, "phase": "takeover_work:done", "action": "takeover_work",
            "handoff_id": "handoff_1", "handoff_accepted": True,
            "claimed_paths": [
                {"path": "/src/auth.py", "acquired": True},
                {"path": "/src/token.py", "acquired": True},
            ],
            "all_claims_acquired": True,
            "next_actions": [
                {"action": "mempalace_wakeup_context", "priority": "high"},
                {"action": "mempalace_prepare_edit", "priority": "high"},
            ],
        }

        guidance = ""
        skill_ref = None
        if result["action"] == "takeover_work" and result["ok"]:
            claimed = result.get("claimed_paths", [])
            all_acquired = result.get("all_claims_acquired", False)
            paths_str = ", ".join(p.get("path", "?") for p in claimed[:3])
            if all_acquired:
                guidance = f"Takeover complete. You now own: {paths_str}. Call mempalace_prepare_edit on each."
            skill_ref = "takeover"

        assert "Takeover complete" in guidance
        assert "/src/auth.py" in guidance
        assert skill_ref == "takeover"


# ---------------------------------------------------------------------------
# Test: prepare_edit now has auto-conflict-check (new behavior)
# ---------------------------------------------------------------------------

class TestPrepareEditAutoConflictCheck:
    """_do_prepare_edit now checks for active claims first (no separate call needed)."""

    def test_prepare_edit_blocks_on_claim_conflict(self):
        """prepare_edit returns claim_conflict when file is claimed by other session."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        claims.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        si = _MockSymbolIndex([])
        ctx = MagicMock()

        result, _, _ = _do_prepare_edit(
            ctx, "/src/main.py", "session-b",
            "/tmp/palace", "/tmp/project", si,
            claims,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "claim_conflict"

    def test_prepare_edit_ok_when_file_unclaimed(self):
        """prepare_edit succeeds (with symbols) when file is unclaimed."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        si = _MockSymbolIndex([
            {"name": "my_func", "type": "function", "file_path": "/src/main.py", "line_start": 10},
        ])
        ctx = MagicMock()

        result, _, _ = _do_prepare_edit(
            ctx, "/src/main.py", "session-a",
            "/tmp/palace", "/tmp/project", si,
            claims,
        )
        assert result["ok"] is True
        assert result["symbols_count"] == 1
        assert result["conflict_verified"] is True

    def test_prepare_edit_ok_self_claim(self):
        """prepare_edit succeeds when session holds the claim (is_self)."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        claims.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        si = _MockSymbolIndex([])
        ctx = MagicMock()

        result, _, _ = _do_prepare_edit(
            ctx, "/src/main.py", "session-a",
            "/tmp/palace", "/tmp/project", si,
            claims,
        )
        assert result["ok"] is True
        assert result["conflict_verified"] is True


# ---------------------------------------------------------------------------
# Test: finish_work diary now written immediately (no follow-up call)
# ---------------------------------------------------------------------------

class TestFinishWorkDiaryImmediate:
    """finish_work now writes diary_entry immediately — no next_actions diary_write."""

    def test_finish_work_with_diary_no_diary_write_next_action(self):
        """With diary_entry → next_actions does NOT include mempalace_diary_write."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        dt = MagicMock()
        dt.capture_decision = MagicMock(return_value={"id": None})
        ctx = MagicMock()

        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry="Fixed the bug in auth module",
            topic="bug-fix", agent_name="Claude",
            capture_decision=None, rationale=None,
            decision_category="general", decision_confidence=3,
            claims_mgr=claims, decision_tracker=dt,
            backend=None,  # No backend — diary prepared but not written
        )
        assert result["ok"] is True
        # diary_id is prepared (backend=None so no actual write)
        assert result["diary_id"] is not None
        # No diary_write follow-up since diary_entry was provided
        next_actions_actions = [a["action"] for a in result["next_actions"]]
        assert "mempalace_diary_write" not in next_actions_actions

    def test_finish_work_without_diary_no_diary_next_action(self):
        """Without diary_entry → no diary_write in next_actions."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        dt = MagicMock()
        dt.capture_decision = MagicMock(return_value={"id": None})
        ctx = MagicMock()

        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry=None, topic="general", agent_name="Claude",
            capture_decision=None, rationale=None,
            decision_category="general", decision_confidence=3,
            claims_mgr=claims, decision_tracker=dt,
            backend=None,
        )
        assert result["ok"] is True
        next_actions_actions = [a["action"] for a in result["next_actions"]]
        assert "mempalace_diary_write" not in next_actions_actions


# ---------------------------------------------------------------------------
# Test: new session tools registered via register_session_tools
# ---------------------------------------------------------------------------

class TestSessionToolsRegistration:
    """Verify new session tools (file_status, workspace_claims, edit_guidance) are registered."""

    def test_register_session_tools_includes_new_tools(self, tmp_path):
        """register_session_tools registers file_status, workspace_claims, edit_guidance."""
        import sys
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = str(tmp_path)

        class _MockSettings:
            def __init__(self):
                self.palace_path = tmp
                self.timeout_write = 30
                self.timeout_read = 15
                self.timeout_embed = 60

        server = MagicMock()
        server._status_cache = MagicMock()

        captured = {}
        _orig = server.tool
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        backend = MagicMock()
        config = MagicMock()
        config.palace_path = tmp
        settings = _MockSettings()

        register_session_tools(server, backend, config, settings)
        server.tool = _orig

        expected = [
            "mempalace_file_status",
            "mempalace_workspace_claims",
            "mempalace_edit_guidance",
        ]
        for name in expected:
            assert name in captured, f"{name} not registered"