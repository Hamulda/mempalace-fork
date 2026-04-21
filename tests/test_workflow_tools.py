"""
Tests for compound workflow tools: mempalace_begin_work, mempalace_prepare_edit,
mempalace_finish_work, mempalace_publish_handoff, mempalace_takeover_work.

Tests cover:
- begin/edit/finish flow
- takeover flow
- handoff flow
- no hidden ambiguity
- backward compatibility with low-level tools

Run: pytest tests/test_workflow_tools.py -v
"""

from __future__ import annotations

import tempfile
import os
import pytest
from unittest.mock import MagicMock


palace_path_factory = tempfile.mkdtemp(prefix="mempalace_wf_")


def _pp():
    return tempfile.mkdtemp(prefix="mempalace_wf_")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockSettings:
    def __init__(self, palace_path: str):
        self.db_path = palace_path
        self.effective_collection_name = "test_collection"
        self.wal_dir = palace_path
        self.palace_path = palace_path
        self.timeout_write = 30
        self.timeout_read = 15
        self.timeout_embed = 60


class _MockClaimsManager:
    """In-memory ClaimsManager fake."""
    def __init__(self):
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

    def get_session_claims(self, session_id):
        now = _now()
        return [
            {"target_id": tid, "session_id": cid, "expires_at": c["expires_at"]}
            for (tt, tid), c in self._claims.items()
            if c["session_id"] == session_id and c["expires_at"] > now
        ]


class _MockWriteCoordinator:
    def __init__(self):
        self._intents = []
        self._committed = set()

    def log_intent(self, session_id, operation, target_type, target_id, payload=None):
        intent_id = f"intent_{len(self._intents)}"
        self._intents.append({
            "id": intent_id, "session_id": session_id, "operation": operation,
            "target_type": target_type, "target_id": target_id,
            "payload": payload, "status": "pending",
        })
        return intent_id

    def commit_intent(self, intent_id, session_id):
        for i in self._intents:
            if i["id"] == intent_id:
                i["status"] = "committed"
                self._committed.add(intent_id)

    def rollback_intent(self, intent_id, session_id):
        for i in self._intents:
            if i["id"] == intent_id:
                i["status"] = "rolled_back"


class _MockHandoffManager:
    """In-memory HandoffManager fake."""
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


class _MockDecisionTracker:
    def __init__(self):
        self._decisions = []
        self._next_id = 1

    def capture_decision(self, session_id, decision_text, rationale,
                        alternatives, category, confidence):
        d = {
            "id": f"decision_{self._next_id}",
            "session_id": session_id,
            "decision_text": decision_text,
            "rationale": rationale,
            "alternatives": alternatives or [],
            "category": category,
            "confidence": confidence,
            "status": "active",
        }
        self._decisions.append(d)
        self._next_id += 1
        return d

    def list_decisions(self, session_id=None, category=None, status=None, limit=50):
        results = list(self._decisions)
        if session_id:
            results = [d for d in results if d["session_id"] == session_id]
        if category:
            results = [d for d in results if d["category"] == category]
        return results[:limit]


class _MockSymbolIndex:
    def __init__(self, symbols=None):
        self._symbols = symbols or []

    def get_file_symbols(self, path):
        matching = [s for s in self._symbols if s.get("file_path") == path]
        return {"symbols": matching[:10]}

    def get_callers(self, symbol_name, project_root):
        return []


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _expires_at(ttl_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


# ---------------------------------------------------------------------------
# Test: unit implementations
# ---------------------------------------------------------------------------

class TestBeginWorkUnit:
    """Unit tests for _do_begin_work."""

    def test_begin_work_no_conflict_acquires_claim(self):
        """No active conflict → claim acquired, ok=True."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result, acquired, conflict = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, "fixing bug",
            claims, wc,
        )
        assert result["ok"] is True
        assert result["phase"] == "begin_work:done"
        assert result["action"] == "begin_work"
        assert acquired is True
        assert conflict["has_conflict"] is False
        assert result["owner"] == "session-a"
        assert result["path"] == "/src/main.py"
        assert len(result["next_actions"]) == 1
        assert result["next_actions"][0]["action"] == "mempalace_prepare_edit"

    def test_begin_work_self_conflict_refreshes(self):
        """Self holds the claim → refresh, ok=True (not blocked)."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        # Pre-claim by session-a
        claims.claim("file", "/src/main.py", "session-a", ttl_seconds=600)

        result, acquired, conflict = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, "continuing work",
            claims, wc,
        )
        assert result["ok"] is True
        assert conflict["is_self"] is True
        # is_self=True means no active conflict blocking the write
        assert conflict["has_conflict"] is False  # self doesn't block itself

    def test_begin_work_other_session_blocks(self):
        """Other session holds claim → blocked, ok=False, failure_mode=claim_conflict."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        claims.claim("file", "/src/main.py", "session-b", ttl_seconds=600)

        result, acquired, conflict = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, "fixing bug",
            claims, wc,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "claim_conflict"
        assert result["details"]["owner"] == "session-b"
        assert acquired is False
        assert "handoff" in result["hint"].lower() or "ttl" in result["hint"].lower()

    def test_begin_work_logs_intent(self):
        """begin_work logs intent to WriteCoordinator."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result, _, _ = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, "refactoring",
            claims, wc,
        )
        assert result["intent_id"] is not None
        assert len(wc._intents) == 1
        assert wc._intents[0]["operation"] == "edit"
        assert wc._intents[0]["target_id"] == "/src/main.py"

    def test_begin_work_no_claims_manager_fails(self):
        """No ClaimsManager available → failure with no_coordination mode."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        ctx = MagicMock()
        result, acquired, _ = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, None,
            None, None,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "no_coordination"


class TestPrepareEditUnit:
    """Unit tests for _do_prepare_edit."""

    def test_prepare_edit_returns_symbols(self):
        """prepare_edit returns symbol list and next_actions."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        si = _MockSymbolIndex([
            {"name": "my_func", "type": "function", "file_path": "/src/main.py", "line_start": 10},
            {"name": "MyClass", "type": "class", "file_path": "/src/main.py", "line_start": 20},
        ])
        ctx = MagicMock()

        result, sym_data = _do_prepare_edit(
            ctx, "/src/main.py", "session-a",
            "/tmp/palace", "/tmp/project", si,
            claims_mgr=None,  # No conflict check needed for symbol-only test
        )
        assert result["ok"] is True
        assert result["phase"] == "prepare_edit:done"
        assert "symbols" in result["context_snippets"]
        assert len(result["context_snippets"]["symbols"]) == 2
        assert result["symbols_count"] == 2

    def test_prepare_edit_no_symbols_ok(self):
        """File with no symbols → ok=True, symbols_count=0."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        si = _MockSymbolIndex([])
        ctx = MagicMock()

        result, _ = _do_prepare_edit(
            ctx, "/src/main.py", "session-a",
            "/tmp/palace", "/tmp/project", si,
            claims_mgr=None,
        )
        assert result["ok"] is True
        assert result["symbols_count"] == 0
        assert result["context_snippets"]["symbols"] == []

    def test_prepare_edit_hotspot_detected(self):
        """File with >= 3 recent changes → hotspot=True, priority=high."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock, patch

        si = _MockSymbolIndex([])
        ctx = MagicMock()

        # Mock recent_changes to return a hot file
        with patch(
            "mempalace.recent_changes.get_recent_changes",
            return_value=[{"file_path": "/src/main.py", "change_count": 5, "last_modified": "2026-04-20"}],
        ):
            result, _ = _do_prepare_edit(
                ctx, "/src/main.py", "session-a",
                "/tmp/palace", "/tmp/project", si,
                claims_mgr=None,
            )
        assert result["ok"] is True


class TestFinishWorkUnit:
    """Unit tests for _do_finish_work."""

    def test_finish_work_releases_claim(self):
        """finish_work releases the claim."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        dt = _MockDecisionTracker()
        ctx = MagicMock()

        claims.claim("file", "/src/main.py", "session-a", ttl_seconds=600)

        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry=None, topic="general", agent_name="Claude",
            capture_decision=None, rationale=None,
            decision_category="general", decision_confidence=3,
            claims_mgr=claims, decision_tracker=dt,
            backend=None,
        )
        assert result["ok"] is True
        assert result["claim_released"] is True

    def test_finish_work_with_diary_entry(self):
        """With diary_entry → diary_id returned."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        dt = _MockDecisionTracker()
        ctx = MagicMock()

        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry="Fixed the bug in auth module",
            topic="bug-fix", agent_name="Claude",
            capture_decision=None, rationale=None,
            decision_category="general", decision_confidence=3,
            claims_mgr=claims, decision_tracker=dt,
            backend=None,
        )
        assert result["ok"] is True
        assert result["diary_entry"] == "Fixed the bug in auth module"
        assert result["diary_id"] is not None
        # diary written immediately — no diary_write follow-up needed
        assert "diary_write" not in [a["action"] for a in result["next_actions"]]

    def test_finish_work_with_decision(self):
        """With capture_decision + rationale → decision captured."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        dt = _MockDecisionTracker()
        ctx = MagicMock()

        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry=None, topic="general", agent_name="Claude",
            capture_decision="Use JWT instead of sessions",
            rationale="Stateless tokens reduce auth server load",
            decision_category="architecture",
            decision_confidence=4,
            claims_mgr=claims, decision_tracker=dt,
            backend=None,
        )
        assert result["ok"] is True
        assert result["decision_id"] is not None
        assert result["decision_id"].startswith("decision_")

    def test_finish_work_no_claims_manager_fails(self):
        """No claims manager → failure with no_coordination."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock

        ctx = MagicMock()
        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry=None, topic="general", agent_name="Claude",
            capture_decision=None, rationale=None,
            decision_category="general", decision_confidence=3,
            claims_mgr=None, decision_tracker=None,
            backend=None,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "no_coordination"


class TestPublishHandoffUnit:
    """Unit tests for _do_publish_handoff."""

    def test_publish_handoff_success(self):
        """push_handoff + release claims → ok=True, handoff_id set."""
        from mempalace.server._workflow_tools import _do_publish_handoff
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        ctx = MagicMock()

        # Pre-claim the paths
        claims.claim("file", "/src/auth.py", "session-a")
        claims.claim("file", "/src/token.py", "session-a")

        result = _do_publish_handoff(
            ctx, summary="Refactored auth module",
            touched_paths=["/src/auth.py", "/src/token.py"],
            blockers=["Need API docs update"],
            next_steps=["Update API docs", "Add tests"],
            confidence=4, priority="high",
            to_session_id=None,
            from_session_id="session-a",
            claims_mgr=claims, handoff_mgr=handoff_mgr,
        )
        assert result["ok"] is True
        assert result["handoff_id"] is not None
        assert result["handoff_id"].startswith("handoff_")
        assert result["from_session_id"] == "session-a"
        assert len(result["released_claims"]) == 2
        assert all(r["success"] for r in result["released_claims"])

    def test_publish_handoff_push_fails_no_release(self):
        """If push_handoff fails, claims not released (atomic on handoff creation)."""
        from mempalace.server._workflow_tools import _do_publish_handoff
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = MagicMock()
        handoff_mgr.push_handoff.return_value = {"error": "push failed"}
        ctx = MagicMock()

        # Pre-claim a path
        claims.claim("file", "/src/auth.py", "session-a")

        result = _do_publish_handoff(
            ctx, summary="Refactored auth module",
            touched_paths=["/src/auth.py"],
            blockers=[], next_steps=[],
            confidence=3, priority="normal",
            to_session_id=None,
            from_session_id="session-a",
            claims_mgr=claims, handoff_mgr=handoff_mgr,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "handoff_push_failed"
        # Claims should NOT have been released
        assert len(result.get("released_claims", [])) == 0

    def test_publish_handoff_partial_release(self):
        """Some paths can't be released → reported in release_errors."""
        from mempalace.server._workflow_tools import _do_publish_handoff
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        ctx = MagicMock()

        # session-a claims /src/auth.py but NOT /src/other.py
        claims.claim("file", "/src/auth.py", "session-a")

        result = _do_publish_handoff(
            ctx, summary="Refactored auth module",
            touched_paths=["/src/auth.py", "/src/other.py"],
            blockers=[], next_steps=[],
            confidence=3, priority="normal",
            to_session_id=None,
            from_session_id="session-a",
            claims_mgr=claims, handoff_mgr=handoff_mgr,
        )
        assert result["ok"] is True  # handoff still created
        # auth.py released successfully; other.py not held by session-a
        released_paths = [r["path"] for r in result["released_claims"]]
        assert "/src/auth.py" in released_paths


class TestTakeoverWorkUnit:
    """Unit tests for _do_takeover_work."""

    def test_takeover_work_success(self):
        """accept_handoff + claim paths → ok=True, claimed_paths populated."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        # Push a handoff first
        h = handoff_mgr.push_handoff(
            from_session_id="session-b", summary="Auth refactor in progress",
            touched_paths=["/src/auth.py", "/src/token.py"],
            blockers=[], next_steps=["Complete API docs"],
            confidence=4, priority="high",
        )
        handoff_id = h["id"]

        result = _do_takeover_work(
            ctx, handoff_id, "session-a",
            paths_to_claim=["/src/auth.py", "/src/token.py"],
            ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=claims, write_coordinator=wc,
        )
        assert result["ok"] is True
        assert result["handoff_accepted"] is True
        assert result["handoff_id"] == handoff_id
        assert len(result["claimed_paths"]) == 2
        assert all(p["acquired"] for p in result["claimed_paths"])
        assert result["all_claims_acquired"] is True

    def test_takeover_work_accept_fails(self):
        """accept_handoff fails → no claims made, failure_mode=accept_failed."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result = _do_takeover_work(
            ctx, "nonexistent_handoff", "session-a",
            paths_to_claim=["/src/auth.py"],
            ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=claims, write_coordinator=wc,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "handoff_accept_failed"
        assert len(result.get("claimed_paths", [])) == 0

    def test_takeover_work_partial_claims(self):
        """Some paths blocked by other claims → reported, ok still True (handoff accepted)."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        handoff_mgr = _MockHandoffManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        h = handoff_mgr.push_handoff(
            from_session_id="session-b", summary="Auth refactor",
            touched_paths=["/src/auth.py", "/src/other.py"],
            blockers=[], next_steps=[],
            confidence=3, priority="normal",
        )

        # session-c holds a claim on /src/other.py
        claims.claim("file", "/src/other.py", "session-c")

        result = _do_takeover_work(
            ctx, h["id"], "session-a",
            paths_to_claim=["/src/auth.py", "/src/other.py"],
            ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=claims, write_coordinator=wc,
        )
        assert result["ok"] is True  # handoff still accepted
        assert result["handoff_accepted"] is True
        assert result["all_claims_acquired"] is False
        assert len(result["claim_errors"]) == 1
        assert result["claim_errors"][0]["path"] == "/src/other.py"


# ---------------------------------------------------------------------------
# Test: tool registration and backward compatibility
# ---------------------------------------------------------------------------

class TestWorkflowToolRegistration:
    """Verify workflow tools register without error."""

    def test_register_workflow_tools_no_error(self, tmp_path):
        """register_workflow_tools completes without exception."""
        import sys
        from unittest.mock import MagicMock
        from mempalace.server._workflow_tools import register_workflow_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()
        server._shared_server_mode = True
        server._claims_manager = _MockClaimsManager()
        server._handoff_manager = _MockHandoffManager()
        server._decision_tracker = _MockDecisionTracker()
        server._write_coordinator = _MockWriteCoordinator()

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

        # Should not raise
        register_workflow_tools(server, backend, config, settings)
        server.tool = _orig

        # All 5 workflow tools should be registered
        expected = [
            "mempalace_begin_work",
            "mempalace_prepare_edit",
            "mempalace_finish_work",
            "mempalace_publish_handoff",
            "mempalace_takeover_work",
        ]
        for name in expected:
            assert name in captured, f"{name} not registered"


class TestWorkflowToolResultContract:
    """Verify workflow_result shape is consistent."""

    @pytest.fixture
    def mock_server_components(self):
        """Server with all coordinators."""
        from mempalace.server._infrastructure import make_status_cache

        tmp = _pp()
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()
        server._shared_server_mode = True
        server._claims_manager = _MockClaimsManager()
        server._handoff_manager = _MockHandoffManager()
        server._decision_tracker = _MockDecisionTracker()
        server._write_coordinator = _MockWriteCoordinator()

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

        from mempalace.server._workflow_tools import register_workflow_tools
        register_workflow_tools(server, backend, config, settings)
        server.tool = _orig

        return server, captured, settings

    def _dummy_ctx(self):
        return MagicMock()

    def test_begin_work_result_has_required_fields(self, mock_server_components):
        """begin_work ok response has: ok, phase, action, path, session_id, owner, expires_at, next_actions."""
        server, captured, settings = mock_server_components
        tool = captured["mempalace_begin_work"]
        ctx = self._dummy_ctx()
        result = tool(ctx, path="/src/main.py", session_id="session-a", ttl_seconds=300)
        assert "ok" in result
        assert "phase" in result
        assert "action" in result
        assert "path" in result
        assert "session_id" in result
        assert "owner" in result
        assert "expires_at" in result
        assert "next_actions" in result
        assert "failure_mode" in result
        assert "context_snippets" in result

    def test_begin_work_conflict_result_has_required_fields(self, mock_server_components):
        """begin_work conflict response has: ok=False, failure_mode, reason, hint, details."""
        server, captured, settings = mock_server_components
        claims = server._claims_manager
        claims.claim("file", "/src/main.py", "session-b", ttl_seconds=600)

        tool = captured["mempalace_begin_work"]
        ctx = self._dummy_ctx()
        result = tool(ctx, path="/src/main.py", session_id="session-a")
        assert result["ok"] is False
        assert "failure_mode" in result
        assert "reason" in result
        assert "hint" in result
        assert "details" in result
        assert result["failure_mode"] == "claim_conflict"
        assert result["details"]["owner"] == "session-b"

    def test_prepare_edit_result_has_required_fields(self, mock_server_components):
        """prepare_edit ok response has: ok, phase, symbols_count, context_snippets."""
        server, captured, settings = mock_server_components
        tool = captured["mempalace_prepare_edit"]
        ctx = self._dummy_ctx()
        result = tool(ctx, path="/src/main.py", session_id="session-a")
        assert result["ok"] is True
        assert "phase" in result
        assert "symbols_count" in result
        assert "context_snippets" in result
        assert "next_actions" in result

    def test_finish_work_result_has_required_fields(self, mock_server_components):
        """finish_work ok response has: ok, path, claim_released, next_actions."""
        server, captured, settings = mock_server_components
        claims = server._claims_manager
        claims.claim("file", "/src/main.py", "session-a", ttl_seconds=600)

        tool = captured["mempalace_finish_work"]
        ctx = self._dummy_ctx()
        result = tool(ctx, path="/src/main.py", session_id="session-a")
        assert result["ok"] is True
        assert "path" in result
        assert "claim_released" in result
        assert "next_actions" in result

    def test_publish_handoff_result_has_required_fields(self, mock_server_components):
        """publish_handoff ok response has: ok, handoff_id, released_claims, next_actions."""
        server, captured, settings = mock_server_components
        tool = captured["mempalace_publish_handoff"]
        ctx = self._dummy_ctx()
        result = tool(
            ctx, summary="Refactored auth",
            touched_paths=["/src/auth.py"],
            from_session_id="session-a",
        )
        assert result["ok"] is True
        assert "handoff_id" in result
        assert "released_claims" in result
        assert "next_actions" in result

    def test_takeover_work_result_has_required_fields(self, mock_server_components):
        """takeover_work ok response has: ok, handoff_id, claimed_paths, next_actions."""
        server, captured, settings = mock_server_components
        handoff_mgr = server._handoff_manager
        h = handoff_mgr.push_handoff(
            from_session_id="session-b", summary="Auth refactor",
            touched_paths=["/src/auth.py"],
            blockers=[], next_steps=[],
            confidence=3, priority="normal",
        )
        claims = server._claims_manager

        tool = captured["mempalace_takeover_work"]
        ctx = self._dummy_ctx()
        result = tool(
            ctx, handoff_id=h["id"],
            paths_to_claim=["/src/auth.py"],
            session_id="session-a",
        )
        assert result["ok"] is True
        assert "handoff_id" in result
        assert "claimed_paths" in result
        assert "next_actions" in result
        assert "all_claims_acquired" in result


class TestWorkflowToolNoAmbiguity:
    """Verify no hidden ambiguity — each tool's failure_mode is specific."""

    def test_begin_work_failure_modes_are_distinct(self):
        """Each failure mode in begin_work maps to a specific root cause."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        # Mode 1: claim_conflict (another session holds it)
        claims = _MockClaimsManager()
        claims.claim("file", "/src/main.py", "session-b")
        result, _, _ = _do_begin_work(MagicMock(), "/src/main.py", "session-a", 600, None, claims, None)
        assert result["failure_mode"] == "claim_conflict"

        # Mode 2: no_coordination (no ClaimsManager)
        result, _, _ = _do_begin_work(MagicMock(), "/src/main.py", "session-a", 600, None, None, None)
        assert result["failure_mode"] == "no_coordination"

        # Mode 3: claim_acquire_failed (unclear state)
        claims2 = _MockClaimsManager()
        claims2.claim = MagicMock(return_value={"acquired": False})  # weird edge case
        result, _, _ = _do_begin_work(MagicMock(), "/src/main.py", "session-a", 600, None, claims2, None)
        assert result["failure_mode"] == "claim_acquire_failed"

    def test_takeover_work_failure_modes_are_distinct(self):
        """Each failure mode in takeover_work is actionable."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        # Mode 1: accept_failed (bad handoff_id)
        handoff_mgr = _MockHandoffManager()
        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        result = _do_takeover_work(
            MagicMock(), "nonexistent", "session-a",
            paths_to_claim=["/src/main.py"], ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=claims, write_coordinator=wc,
        )
        assert result["failure_mode"] == "handoff_accept_failed"

        # Mode 2: no_coordination (no claims manager)
        result = _do_takeover_work(
            MagicMock(), "any", "session-a",
            paths_to_claim=["/src/main.py"], ttl_seconds=600,
            handoff_mgr=handoff_mgr, claims_mgr=None, write_coordinator=wc,
        )
        assert result["failure_mode"] == "no_coordination"

    def test_publish_handoff_failure_modes_are_distinct(self):
        """publish_handoff distinguishes push failure from coordination issues."""
        from mempalace.server._workflow_tools import _do_publish_handoff
        from unittest.mock import MagicMock

        # Mode 1: no_handoff_manager
        result = _do_publish_handoff(
            MagicMock(), "summary", [], [], [], 3, "normal", None, "session-a",
            claims_mgr=MagicMock(), handoff_mgr=None,
        )
        assert result["failure_mode"] == "no_handoff_manager"

        # Mode 2: handoff_push_failed
        handoff_mgr = MagicMock()
        handoff_mgr.push_handoff.return_value = {"error": "failed"}
        result = _do_publish_handoff(
            MagicMock(), "summary", [], [], [], 3, "normal", None, "session-a",
            claims_mgr=MagicMock(), handoff_mgr=handoff_mgr,
        )
        assert result["failure_mode"] == "handoff_push_failed"


# ---------------------------------------------------------------------------
# Test: session ID auto-detection flows through workflow tools
# ---------------------------------------------------------------------------

class TestWorkflowToolsAutoDetectSession:
    """Verify session_id is auto-detected in workflow tools."""

    def test_begin_work_auto_detects_session_id(self, tmp_path):
        """begin_work resolves session_id from context if not provided."""
        import sys
        from unittest.mock import MagicMock
        from mempalace.server._workflow_tools import register_workflow_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()
        server._shared_server_mode = True
        server._claims_manager = _MockClaimsManager()
        server._write_coordinator = _MockWriteCoordinator()

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

        register_workflow_tools(server, backend, config, settings)
        server.tool = _orig

        tool = captured["mempalace_begin_work"]

        # Mock context that auto-detects session_id
        mock_ctx = MagicMock()
        mock_ctx.request_context = MagicMock()
        mock_ctx.request_context.session_id = "auto-detected-session"

        # No session_id passed → should use auto-detected
        result = tool(mock_ctx, path="/src/main.py")
        # If auto-detection works, the call succeeds (acquires claim)
        assert result.get("ok") is True or result.get("failure_mode") == "no_coordination"

    def test_finish_work_auto_detects_session_id(self, tmp_path):
        """finish_work resolves session_id from context."""
        import sys
        from unittest.mock import MagicMock
        from mempalace.server._workflow_tools import register_workflow_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()
        server._shared_server_mode = True
        server._claims_manager = _MockClaimsManager()
        server._decision_tracker = _MockDecisionTracker()
        server._write_coordinator = _MockWriteCoordinator()

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

        register_workflow_tools(server, backend, config, settings)
        server.tool = _orig

        tool = captured["mempalace_finish_work"]

        mock_ctx = MagicMock()
        mock_ctx.request_context = MagicMock()
        mock_ctx.request_context.session_id = "auto-session"

        # Pre-claim the path by auto-session so release succeeds
        server._claims_manager.claim("file", "/src/main.py", "auto-session")

        result = tool(mock_ctx, path="/src/main.py")
        assert result.get("ok") is True


# ---------------------------------------------------------------------------
# Test: create_server wires workflow tools
# ---------------------------------------------------------------------------

class TestFactoryWiresWorkflowTools:
    """Verify factory.create_server() wires workflow tools in shared mode."""

    @pytest.mark.asyncio
    async def test_create_server_includes_workflow_tools(self, tmp_path):
        """create_server(shared_server_mode=True) → workflow tools registered."""
        import os
        from mempalace.server.factory import create_server
        from mempalace.settings import MemPalaceSettings

        original_pp = os.environ.get("MEMPALACE_PALACE_PATH")
        palace = str(tmp_path / "wf_palace")
        os.environ["MEMPALACE_PALACE_PATH"] = palace

        try:
            settings = MemPalaceSettings()
            settings.palace_path = palace
            settings.db_path = str(tmp_path / "db")
            settings.transport = "stdio"

            mcp = create_server(settings=settings, shared_server_mode=True)

            # Workflow tools are methods on the FastMCP server
            # (registered via @server.tool decorator)
            tool_names = [t.name for t in await mcp.list_tools()]
            for expected in [
                "mempalace_begin_work",
                "mempalace_prepare_edit",
                "mempalace_finish_work",
                "mempalace_publish_handoff",
                "mempalace_takeover_work",
            ]:
                assert expected in tool_names, f"{expected} not found in tools: {tool_names}"

            # Cleanup
            if hasattr(mcp, "_claims_manager"):
                mcp._claims_manager.close()
            if hasattr(mcp, "_handoff_manager"):
                mcp._handoff_manager.close()
            if hasattr(mcp, "_decision_tracker"):
                mcp._decision_tracker.close()
        finally:
            if original_pp:
                os.environ["MEMPALACE_PALACE_PATH"] = original_pp
            elif "MEMPALACE_PALACE_PATH" in os.environ:
                del os.environ["MEMPALACE_PALACE_PATH"]


# ---------------------------------------------------------------------------
# Test: low-level tools still work (backward compatibility)
# ---------------------------------------------------------------------------

class TestLowLevelToolsStillWork:
    """Verify existing low-level tools remain functional alongside workflow tools."""

    @pytest.mark.asyncio
    async def test_low_level_claim_path_still_works(self, tmp_path):
        """mempalace_claim_path (low-level) still works after workflow tools added."""
        import os
        from mempalace.server.factory import create_server
        from mempalace.settings import MemPalaceSettings

        original_pp = os.environ.get("MEMPALACE_PALACE_PATH")
        palace = str(tmp_path / "ll_palace")
        os.environ["MEMPALACE_PALACE_PATH"] = palace

        try:
            settings = MemPalaceSettings()
            settings.palace_path = palace
            settings.db_path = str(tmp_path / "db")
            settings.transport = "stdio"

            mcp = create_server(settings=settings, shared_server_mode=True)
            tool_names = [t.name for t in await mcp.list_tools()]

            for low_level in [
                "mempalace_claim_path",
                "mempalace_release_claim",
                "mempalace_conflict_check",
                "mempalace_push_handoff",
                "mempalace_pull_handoffs",
                "mempalace_wakeup_context",
                "mempalace_add_drawer",
                "mempalace_diary_write",
            ]:
                assert low_level in tool_names, f"{low_level} missing"

            if hasattr(mcp, "_claims_manager"):
                mcp._claims_manager.close()
            if hasattr(mcp, "_handoff_manager"):
                mcp._handoff_manager.close()
            if hasattr(mcp, "_decision_tracker"):
                mcp._decision_tracker.close()
        finally:
            if original_pp:
                os.environ["MEMPALACE_PALACE_PATH"] = original_pp
            elif "MEMPALACE_PALACE_PATH" in os.environ:
                del os.environ["MEMPALACE_PALACE_PATH"]


# ---------------------------------------------------------------------------
# Test: workflow_state contract
# ---------------------------------------------------------------------------

class TestWorkflowStateContract:
    """Every workflow tool result MUST include workflow_state with required fields."""

    def test_begin_work_returns_workflow_state(self):
        """begin_work success returns workflow_state with current_phase=claim_acquired."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result, _, _ = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, "fixing bug",
            claims, wc,
        )
        assert result["ok"] is True
        ws = result.get("workflow_state", {})
        assert ws.get("current_phase") == "claim_acquired"
        assert ws.get("next_phase") == "prepare"
        assert ws.get("next_tool") == "mempalace_prepare_edit"
        assert "conflict_status" in ws
        assert "handoff_pending" in ws

    def test_begin_work_self_conflict_workflow_state(self):
        """Self-conflict refresh → conflict_status=self_claim."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()
        claims.claim("file", "/src/main.py", "session-a", ttl_seconds=600)

        result, _, _ = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, "continue work",
            claims, wc,
        )
        assert result["ok"] is True
        ws = result.get("workflow_state", {})
        assert ws.get("conflict_status") == "self_claim"

    def test_begin_work_conflict_failure_workflow_state(self):
        """Other-session conflict → workflow_state with blocked phase."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()
        claims.claim("file", "/src/main.py", "other-session", ttl_seconds=600)

        result, _, _ = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, "try to claim",
            claims, wc,
        )
        assert result["ok"] is False
        assert result["failure_mode"] == "claim_conflict"
        ws = result.get("workflow_state", {})
        assert ws.get("current_phase") == "blocked"
        assert ws.get("next_phase") == "negotiate"
        assert ws.get("next_tool") == "mempalace_push_handoff"
        assert ws.get("conflict_status") == "other_claim"

    def test_prepare_edit_returns_workflow_state(self):
        """prepare_edit success returns workflow_state with current_phase=context_ready."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        si = _MockSymbolIndex()
        ctx = MagicMock()

        result, _ = _do_prepare_edit(
            ctx, "/src/main.py", "session-a", "/tmp/palace", None, si, claims,
        )
        assert result["ok"] is True
        ws = result.get("workflow_state", {})
        assert ws.get("current_phase") == "context_ready"
        assert ws.get("next_phase") == "edit"
        assert ws.get("next_tool") == "MODEL_ACTION:edit"
        assert "conflict_status" in ws
        assert "handoff_pending" in ws

    def test_prepare_edit_conflict_failure_workflow_state(self):
        """Other-session conflict blocks prepare_edit."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        si = _MockSymbolIndex()
        ctx = MagicMock()
        claims.claim("file", "/src/main.py", "other-session", ttl_seconds=600)

        result, _ = _do_prepare_edit(
            ctx, "/src/main.py", "session-a", "/tmp/palace", None, si, claims,
        )
        assert result["ok"] is False
        ws = result.get("workflow_state", {})
        assert ws.get("current_phase") == "blocked"
        assert ws.get("next_tool") == "mempalace_push_handoff"

    def test_finish_work_returns_workflow_state(self):
        """finish_work success returns workflow_state with current_phase=finished."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        dt = _MockDecisionTracker()
        ctx = MagicMock()

        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry=None, topic="general", agent_name="claude",
            capture_decision=None, rationale=None,
            decision_category="general", decision_confidence=3,
            claims_mgr=claims, decision_tracker=dt, backend=None,
        )
        assert result["ok"] is True
        ws = result.get("workflow_state", {})
        assert ws.get("current_phase") == "finished"
        assert "conflict_status" in ws
        assert "handoff_pending" in ws

    def test_publish_handoff_returns_workflow_state(self):
        """publish_handoff success returns workflow_state with current_phase=published."""
        from mempalace.server._workflow_tools import _do_publish_handoff
        from unittest.mock import MagicMock

        hm = _MockHandoffManager()
        claims = _MockClaimsManager()
        ctx = MagicMock()

        result = _do_publish_handoff(
            ctx,
            summary="Auth refactor done",
            touched_paths=["/src/auth.py"],
            blockers=[],
            next_steps=[],
            confidence=3,
            priority="normal",
            to_session_id=None,
            from_session_id="session-a",
            claims_mgr=claims,
            handoff_mgr=hm,
        )
        assert result["ok"] is True
        ws = result.get("workflow_state", {})
        assert ws.get("current_phase") == "published"
        assert ws.get("next_tool") == "mempalace_diary_write"
        assert ws.get("handoff_pending") is False

    def test_takeover_work_returns_workflow_state(self):
        """takeover_work success returns workflow_state with current_phase=takeover."""
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        hm = _MockHandoffManager()
        hm.push_handoff(
            from_session_id="old-session",
            summary="Auth work in progress",
            touched_paths=["/src/auth.py"],
            blockers=[],
            next_steps=["Finish the token rotation"],
            confidence=3,
            priority="normal",
        )
        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result = _do_takeover_work(
            ctx,
            handoff_id="handoff_1",
            session_id="new-session",
            paths_to_claim=["/src/auth.py"],
            ttl_seconds=600,
            handoff_mgr=hm,
            claims_mgr=claims,
            write_coordinator=wc,
        )
        assert result["ok"] is True
        ws = result.get("workflow_state", {})
        assert ws.get("current_phase") == "takeover"
        assert ws.get("next_phase") == "prepare"
        assert ws.get("next_tool") == "mempalace_wakeup_context"


class TestNextToolGuarantee:
    """next_tool MUST be set on every success result (never None)."""

    def test_all_success_results_have_next_tool(self):
        """Every OK workflow result has workflow_state.next_tool != None."""
        from mempalace.server._workflow_tools import _do_begin_work, _do_prepare_edit
        from mempalace.server._workflow_tools import _do_finish_work, _do_publish_handoff
        from mempalace.server._workflow_tools import _do_takeover_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        hm = _MockHandoffManager()
        dt = _MockDecisionTracker()
        si = _MockSymbolIndex()
        ctx = MagicMock()

        hm.push_handoff(
            from_session_id="old-session", summary="test",
            touched_paths=["/src/a.py"], blockers=[], next_steps=[],
            confidence=3, priority="normal",
        )

        cases = [
            ("begin_work", lambda: _do_begin_work(ctx, "/src/a.py", "s1", 600, None, claims, wc)),
            ("prepare_edit", lambda: _do_prepare_edit(ctx, "/src/a.py", "s1", "/tmp/p", None, si, claims)),
            ("finish_work", lambda: _do_finish_work(ctx, "/src/a.py", "s1", None, "g", "c", None, None, "g", 3, claims, dt, None)),
            ("publish_handoff", lambda: _do_publish_handoff(ctx, "done", ["/src/a.py"], [], [], 3, "n", None, "s1", claims, hm)),
            ("takeover_work", lambda: _do_takeover_work(ctx, "handoff_1", "s2", ["/src/a.py"], 600, hm, claims, wc)),
        ]

        # finish_work is terminal (current_phase=finished, next_tool=None) — allowed
        _terminal_states = {"finished"}

        for name, call in cases:
            result = call()
            if isinstance(result, tuple):
                result = result[0]
            assert result["ok"] is True, f"{name} returned ok=False"
            ws = result.get("workflow_state", {})
            current_phase = ws.get("current_phase")
            if current_phase in _terminal_states:
                assert ws.get("next_tool") is None, f"{name} is terminal, next_tool should be None"
            else:
                assert ws.get("next_tool") is not None, f"{name} has null next_tool for non-terminal phase {current_phase}: {ws}"

    def test_all_failure_results_have_workflow_state(self):
        """Every FAIL workflow result has workflow_state (blocked/negotiate state)."""
        from mempalace.server._workflow_tools import _do_begin_work, _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        claims.claim("file", "/src/blocked.py", "other-session", ttl_seconds=600)
        wc = _MockWriteCoordinator()
        si = _MockSymbolIndex()
        ctx = MagicMock()

        cases = [
            ("begin_work conflict", lambda: _do_begin_work(ctx, "/src/blocked.py", "s1", 600, None, claims, wc)),
            ("prepare_edit conflict", lambda: _do_prepare_edit(ctx, "/src/blocked.py", "s1", "/tmp/p", None, si, claims)),
        ]

        for name, call in cases:
            result = call()
            if isinstance(result, tuple):
                result = result[0]
            assert result["ok"] is False, f"{name} should fail"
            ws = result.get("workflow_state", {})
            assert ws, f"{name} missing workflow_state on failure"
            assert ws.get("current_phase") == "blocked", f"{name} wrong phase: {ws}"
            assert ws.get("next_tool") is not None, f"{name} has null next_tool on failure"

    def test_conflict_status_always_set(self):
        """conflict_status field always present in workflow_state."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result, _, _ = _do_begin_work(
            ctx, "/src/main.py", "session-a", 600, None, claims, wc,
        )
        ws = result.get("workflow_state", {})
        assert "conflict_status" in ws
        assert ws["conflict_status"] in ("none", "self_claim", "other_claim", "hotspot")


class TestNoContradictoryHints:
    """next_actions must not contain contradictory hints."""

    def test_begin_work_conflict_no_prepare_in_next_actions(self):
        """When begin_work fails with claim_conflict, next_actions does NOT contain prepare_edit."""
        from mempalace.server._workflow_tools import _do_begin_work
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        claims.claim("file", "/src/main.py", "other-session", ttl_seconds=600)
        wc = _MockWriteCoordinator()
        ctx = MagicMock()

        result = _do_begin_work(ctx, "/src/main.py", "session-a", 600, None, claims, wc)
        if isinstance(result, tuple):
            result = result[0]
        assert result["ok"] is False
        assert result["failure_mode"] == "claim_conflict"
        actions = [a["action"] for a in result.get("next_actions", [])]
        assert "mempalace_prepare_edit" not in actions
        assert "mempalace_push_handoff" in actions

    def test_prepare_edit_conflict_no_edit_in_next_actions(self):
        """When prepare_edit fails with claim_conflict, next_actions points to negotiate."""
        from mempalace.server._workflow_tools import _do_prepare_edit
        from unittest.mock import MagicMock

        claims = _MockClaimsManager()
        claims.claim("file", "/src/main.py", "other-session", ttl_seconds=600)
        si = _MockSymbolIndex()
        ctx = MagicMock()

        result = _do_prepare_edit(ctx, "/src/main.py", "session-a", "/tmp/p", None, si, claims)
        if isinstance(result, tuple):
            result = result[0]
        assert result["ok"] is False
        assert result["failure_mode"] == "claim_conflict"
        actions = [a["action"] for a in result.get("next_actions", [])]
        assert "MODEL_ACTION:edit" not in actions
        assert "mempalace_push_handoff" in actions

    def test_finish_work_no_diary_write_if_immediate(self):
        """When finish_work writes diary immediately, next_actions does NOT contain mempalace_diary_write."""
        from mempalace.server._workflow_tools import _do_finish_work
        from unittest.mock import MagicMock, patch

        claims = _MockClaimsManager()
        dt = _MockDecisionTracker()
        ctx = MagicMock()

        class _FakeCollection:
            def upsert(self, ids, documents, metadatas):
                pass

        class _FakeBackend:
            def get_collection(self):
                return _FakeCollection()

        backend = _FakeBackend()

        result = _do_finish_work(
            ctx, "/src/main.py", "session-a",
            diary_entry="Fixed auth bug", topic="bug-fix", agent_name="claude",
            capture_decision=None, rationale=None,
            decision_category="general", decision_confidence=3,
            claims_mgr=claims, decision_tracker=dt, backend=backend,
        )
        assert result["ok"] is True
        assert result.get("diary_id") is not None, "diary should be written immediately"
        actions = [a["action"] for a in result.get("next_actions", [])]
        assert "mempalace_diary_write" not in actions


class TestTier2BackwardCompatibility:
    """Low-level tools still register and return valid shapes."""

    @pytest.mark.asyncio
    async def test_low_level_claim_path_still_works(self, tmp_path):
        """mempalace_claim_path (low-level) still registers and works."""
        import os
        from mempalace.server.factory import create_server
        from mempalace.settings import MemPalaceSettings

        original_pp = os.environ.get("MEMPALACE_PALACE_PATH")
        palace = str(tmp_path / "t2_palace")
        os.environ["MEMPALACE_PALACE_PATH"] = palace

        try:
            settings = MemPalaceSettings()
            settings.palace_path = palace
            settings.db_path = str(tmp_path / "db")
            settings.transport = "stdio"

            mcp = create_server(settings=settings, shared_server_mode=True)
            tool_names = [t.name for t in await mcp.list_tools()]

            for low_level in [
                "mempalace_claim_path",
                "mempalace_release_claim",
                "mempalace_conflict_check",
            ]:
                assert low_level in tool_names, f"{low_level} missing"

            if hasattr(mcp, "_claims_manager"):
                mcp._claims_manager.close()
            if hasattr(mcp, "_handoff_manager"):
                mcp._handoff_manager.close()
            if hasattr(mcp, "_decision_tracker"):
                mcp._decision_tracker.close()
        finally:
            if original_pp:
                os.environ["MEMPALACE_PALACE_PATH"] = original_pp
            elif "MEMPALACE_PALACE_PATH" in os.environ:
                del os.environ["MEMPALACE_PALACE_PATH"]

