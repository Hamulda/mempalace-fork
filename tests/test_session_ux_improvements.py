"""
Tests for Session UX Improvements — auto-detection, improved wakeup/handoff flows.

Verifies:
1. _get_session_id_from_ctx extracts session_id from FastMCP Context
2. _require_session_id resolves session_id with correct priority (explicit > auto > error)
3. _optional_session_id returns None when nothing available
4. Session tools work without explicit session_id when context has it
5. Explicit session_id overrides auto-detected value (no hidden magic)
6. Backward compatibility: old call patterns still work (no new required params)
7. push_handoff minimal call works (only summary required)
8. capture_decision minimal call works (only decision + rationale required)
9. wakeup_context no-arg call works when session context is available

Run: pytest tests/test_session_ux_improvements.py -v
"""

import os
import pytest
from unittest.mock import MagicMock


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

class _FakeRequestContext:
    """Fake request context that FastMCP Context might expose."""
    def __init__(self, session_id=None):
        self.session_id = session_id


class _FakeFastMCPContext:
    """Fake fastmcp_context attribute that middleware uses."""
    def __init__(self, session_id=None):
        self.session_id = session_id


class _MockClaimsManager:
    """Minimal ClaimsManager fake for session tool tests."""
    def __init__(self):
        self._claims = {}

    def claim(self, target_type, target_id, session_id, ttl_seconds=600, payload=None):
        key = (target_type, target_id)
        self._claims[key] = {
            "session_id": session_id,
            "expires_at": "2099-01-01T00:00:00Z",
            "payload": payload or {},
        }
        return {"acquired": True, "owner": session_id}

    def release_claim(self, target_type, target_id, session_id):
        key = (target_type, target_id)
        if key in self._claims:
            del self._claims[key]
        return {"success": True}

    def get_session_claims(self, session_id):
        return [
            {"target_id": tid, "session_id": cid}
            for (tt, tid), cid in self._claims.items()
            if cid["session_id"] == session_id
        ]

    def list_active_claims(self):
        return [{"target_id": tid, "session_id": cid["session_id"]}
                for (tt, tid), cid in self._claims.items()]

    def check_conflicts(self, target_type, target_id, session_id):
        key = (target_type, target_id)
        c = self._claims.get(key)
        if c is None:
            return {"has_conflict": False}
        if c["session_id"] == session_id:
            return {"has_conflict": False, "is_self": True}
        return {"has_conflict": True, "owner": c["session_id"]}


class _MockHandoffManager:
    """Minimal HandoffManager fake."""
    def __init__(self):
        self._handoffs = []

    def push_handoff(self, from_session_id, summary, touched_paths, blockers,
                     next_steps, confidence, priority, to_session_id):
        h = {
            "handoff_id": "h-1",
            "from_session_id": from_session_id,
            "summary": summary,
            "touched_paths": touched_paths,
            "blockers": blockers,
            "next_steps": next_steps,
            "confidence": confidence,
            "priority": priority,
            "to_session_id": to_session_id,
            "status": "pending",
        }
        self._handoffs.append(h)
        return h

    def pull_handoffs(self, session_id=None, status=None):
        if session_id is None:
            return [h for h in self._handoffs if h["to_session_id"] is None]
        return [h for h in self._handoffs
                if h["from_session_id"] == session_id or h["to_session_id"] == session_id]

    def accept_handoff(self, handoff_id, session_id):
        for h in self._handoffs:
            if h["handoff_id"] == handoff_id:
                h["status"] = "accepted"
                h["accepted_by"] = session_id
                return {"success": True, "handoff": h}
        return {"success": False, "error": "not_found"}

    def complete_handoff(self, handoff_id, session_id):
        for h in self._handoffs:
            if h["handoff_id"] == handoff_id:
                h["status"] = "completed"
                return {"success": True}
        return {"success": False, "error": "not_found"}


class _MockDecisionTracker:
    """Minimal DecisionTracker fake."""
    def __init__(self):
        self._decisions = []

    def capture_decision(self, session_id, decision_text, rationale, alternatives,
                        category, confidence):
        d = {
            "decision_id": "d-1",
            "session_id": session_id,
            "decision": decision_text,
            "rationale": rationale,
            "alternatives": alternatives,
            "category": category,
            "confidence": confidence,
            "status": "active",
        }
        self._decisions.append(d)
        return d

    def list_decisions(self, session_id=None, category=None):
        results = self._decisions
        if session_id is not None:
            results = [d for d in results if d["session_id"] == session_id]
        if category is not None:
            results = [d for d in results if d["category"] == category]
        return results


def _pp():
    import tempfile
    return tempfile.mkdtemp(prefix="mempalace_sux_")


# ────────────────────────────────────────────────────────────────────────────
# Test: _get_session_id_from_ctx
# ────────────────────────────────────────────────────────────────────────────

class TestGetSessionIdFromCtx:
    """Unit tests for session ID extraction from FastMCP Context."""

    def test_returns_session_id_from_request_context(self):
        """request_context.session_id is available → returns it."""
        from mempalace.server._session_tools import _get_session_id_from_ctx
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="session-from-req")
        ctx.fastmcp_context = None

        result = _get_session_id_from_ctx(ctx)
        assert result == "session-from-req"

    def test_falls_back_to_fastmcp_context(self):
        """request_context not available, fastmcp_context.session_id → returns it."""
        from mempalace.server._session_tools import _get_session_id_from_ctx
        ctx = MagicMock()
        ctx.request_context = None
        ctx.fastmcp_context = _FakeFastMCPContext(session_id="session-from-fmcp")

        result = _get_session_id_from_ctx(ctx)
        assert result == "session-from-fmcp"

    def test_falls_back_to_env_var(self):
        """Neither context attribute available → falls back to MEMPALACE_SESSION_ID env."""
        from mempalace.server._session_tools import _get_session_id_from_ctx
        os.environ["MEMPALACE_SESSION_ID"] = "session-from-env"
        try:
            ctx = MagicMock(spec=[])
            ctx.request_context = None
            ctx.fastmcp_context = None

            result = _get_session_id_from_ctx(ctx)
            assert result == "session-from-env"
        finally:
            del os.environ["MEMPALACE_SESSION_ID"]

    def test_returns_none_when_not_available(self):
        """No context attrs and no env var → returns None (no exception)."""
        from mempalace.server._session_tools import _get_session_id_from_ctx
        ctx = MagicMock(spec=[])
        ctx.request_context = None
        ctx.fastmcp_context = None

        result = _get_session_id_from_ctx(ctx)
        assert result is None

    def test_returns_none_on_attribute_error(self):
        """Context raises AttributeError on access → returns None."""
        from mempalace.server._session_tools import _get_session_id_from_ctx

        class _RaiseAttr:
            def __getattr__(self, name):
                raise AttributeError("no attribute")

        ctx = MagicMock()
        ctx.request_context = _RaiseAttr()
        ctx.fastmcp_context = _RaiseAttr()

        result = _get_session_id_from_ctx(ctx)
        assert result is None


# ────────────────────────────────────────────────────────────────────────────
# Test: _require_session_id
# ────────────────────────────────────────────────────────────────────────────

class TestRequireSessionId:
    """Unit tests for _require_session_id helper."""

    def test_explicit_overrides_auto_detected(self):
        """Explicit session_id → returns it even if auto-detected is available."""
        from mempalace.server._session_tools import _require_session_id
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="auto-detected")
        ctx.fastmcp_context = None

        result = _require_session_id(ctx, explicit="explicit-id", action="test")
        assert result == "explicit-id"

    def test_auto_detected_when_no_explicit(self):
        """No explicit → uses auto-detected from context."""
        from mempalace.server._session_tools import _require_session_id
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="auto-detected")
        ctx.fastmcp_context = None

        result = _require_session_id(ctx, explicit=None, action="test")
        assert result == "auto-detected"

    def test_raises_when_no_session_available(self):
        """No explicit, no auto-detected → raises ValueError."""
        from mempalace.server._session_tools import _require_session_id
        ctx = MagicMock(spec=[])
        ctx.request_context = None
        ctx.fastmcp_context = None

        with pytest.raises(ValueError) as exc_info:
            _require_session_id(ctx, None, "my_action")
        assert "my_action" in str(exc_info.value)
        assert "MEMPALACE_SESSION_ID" in str(exc_info.value)


# ────────────────────────────────────────────────────────────────────────────
# Test: _optional_session_id
# ────────────────────────────────────────────────────────────────────────────

class TestOptionalSessionId:
    """Unit tests for _optional_session_id helper."""

    def test_explicit_overrides_auto_detected(self):
        """Explicit → returns it."""
        from mempalace.server._session_tools import _optional_session_id
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="auto-detected")
        ctx.fastmcp_context = None

        result = _optional_session_id(ctx, explicit="explicit")
        assert result == "explicit"

    def test_auto_detected_when_no_explicit(self):
        """No explicit → auto-detected."""
        from mempalace.server._session_tools import _optional_session_id
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="auto-detected")
        ctx.fastmcp_context = None

        result = _optional_session_id(ctx, explicit=None)
        assert result == "auto-detected"

    def test_returns_none_when_not_available(self):
        """Neither available → returns None (no exception)."""
        from mempalace.server._session_tools import _optional_session_id
        ctx = MagicMock(spec=[])
        ctx.request_context = None
        ctx.fastmcp_context = None

        result = _optional_session_id(ctx, explicit=None)
        assert result is None


# ────────────────────────────────────────────────────────────────────────────
# Test: session tools work without explicit session_id
# ────────────────────────────────────────────────────────────────────────────

class TestSessionToolsAutoDetection:
    """Functional tests for session tools with auto-detection."""

    @pytest.fixture
    def tool_server(self):
        """Create a minimal server with all managers attached for tool testing."""
        import sys
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._claims_manager = _MockClaimsManager()
        server._handoff_manager = _MockHandoffManager()
        server._decision_tracker = _MockDecisionTracker()

        config = MagicMock()
        config.palace_path = tmp

        settings = MagicMock()
        settings.timeout_write = 30
        settings.timeout_read = 15

        captured = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator

        server.tool = capture_tool

        register_session_tools(server, MagicMock(), config, settings)

        return server, captured

    def test_claim_path_auto_detects_session_id(self, tool_server):
        """claim_path without session_id → uses auto-detected from ctx."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="session-auto")
        ctx.fastmcp_context = None

        result = captured["mempalace_claim_path"](
            ctx, path="/src/main.py", session_id=None
        )
        assert result["owner"] == "session-auto"

    def test_claim_path_explicit_overrides_auto(self, tool_server):
        """claim_path with explicit session_id → uses explicit even if ctx has one."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="ctx-session")
        ctx.fastmcp_context = None

        result = captured["mempalace_claim_path"](
            ctx, path="/src/main.py", session_id="explicit-session"
        )
        assert result["owner"] == "explicit-session"

    def test_release_claim_auto_detects_session_id(self, tool_server):
        """release_claim without session_id → auto-detected."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="session-release")
        ctx.fastmcp_context = None

        # First claim
        captured["mempalace_claim_path"](
            ctx, path="/src/main.py", session_id="session-release"
        )
        # Then release without explicit session_id
        result = captured["mempalace_release_claim"](ctx, path="/src/main.py")
        assert result["success"] is True

    def test_conflict_check_auto_detects_session_id(self, tool_server):
        """conflict_check without session_id → auto-detected."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="session-conflict")
        ctx.fastmcp_context = None

        result = captured["mempalace_conflict_check"](ctx, path="/src/main.py")
        # No conflict for this session
        assert result.get("has_conflict") is False

    def test_push_handoff_minimal_call(self, tool_server):
        """push_handoff with only summary → uses auto-detected from_session_id."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="handoff-session")
        ctx.fastmcp_context = None

        # Only summary, everything else uses defaults
        result = captured["mempalace_push_handoff"](ctx, summary="Did auth refactor")
        assert result["from_session_id"] == "handoff-session"
        assert result["summary"] == "Did auth refactor"
        assert result["confidence"] == 3          # default
        assert result["priority"] == "normal"      # default
        assert result["touched_paths"] == []       # default
        assert result["blockers"] == []             # default
        assert result["next_steps"] == []           # default

    def test_push_handoff_explicit_from_session_id_overrides(self, tool_server):
        """push_handoff with explicit from_session_id → overrides auto-detection."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="ctx-session")
        ctx.fastmcp_context = None

        result = captured["mempalace_push_handoff"](
            ctx, summary="Test", from_session_id="other-session"
        )
        assert result["from_session_id"] == "other-session"

    def test_pull_handoffs_no_session_pulls_broadcasts(self, tool_server):
        """pull_handoffs without session_id → pulls broadcasts (to_session_id=None)."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = None
        ctx.fastmcp_context = None

        result = captured["mempalace_pull_handoffs"](ctx, session_id=None)
        assert result["count"] == 0  # no handoffs yet

    def test_accept_handoff_auto_detects_session_id(self, tool_server):
        """accept_handoff without session_id → auto-detected."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="acceptor-session")
        ctx.fastmcp_context = None

        # Push a broadcast handoff first
        captured["mempalace_push_handoff"](
            ctx, summary="Broadcast work"
        )

        # Accept without explicit session_id
        result = captured["mempalace_accept_handoff"](
            ctx, handoff_id="h-1"
        )
        assert result["success"] is True
        assert result["handoff"]["accepted_by"] == "acceptor-session"

    def test_complete_handoff_auto_detects_session_id(self, tool_server):
        """complete_handoff without session_id → auto-detected."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="completer-session")
        ctx.fastmcp_context = None

        # Push + accept first
        captured["mempalace_push_handoff"](ctx, summary="Work to complete")
        captured["mempalace_accept_handoff"](ctx, handoff_id="h-1")

        # Complete without explicit session_id
        result = captured["mempalace_complete_handoff"](ctx, handoff_id="h-1")
        assert result["success"] is True

    def test_wakeup_context_auto_detects_session_id(self, tool_server):
        """wakeup_context without session_id → session_id resolved via _require_session_id."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="wakeup-session")
        ctx.fastmcp_context = None

        # We can't directly patch build_wakeup_context (local import inside closure).
        # Instead, verify the error path is NOT triggered (ValueError for missing session)
        # and that when the context has a session, no error is raised.
        # The actual build_wakeup_context call would succeed with the resolved session.
        result = captured["mempalace_wakeup_context"](ctx)
        # If session_id were missing, we'd get an error dict. Since ctx has session context,
        # we either get a real result or an error from build_wakeup_context itself
        # (which would be a different error, not a session_id resolution error).
        assert "error" not in result or "session_id" not in result["error"].lower()

    def test_capture_decision_minimal_call(self, tool_server):
        """capture_decision with only decision + rationale → uses defaults for rest."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="decision-session")
        ctx.fastmcp_context = None

        result = captured["mempalace_capture_decision"](
            ctx,
            decision="Use JWT for auth",
            rationale="Sessions don't scale horizontally"
        )
        assert result["session_id"] == "decision-session"
        assert result["confidence"] == 3        # default
        assert result["alternatives"] == []       # default
        assert result["category"] == "general"   # default

    def test_capture_decision_full_call(self, tool_server):
        """capture_decision with all params → uses provided values."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="decision-session")
        ctx.fastmcp_context = None

        result = captured["mempalace_capture_decision"](
            ctx,
            decision="Use JWT",
            rationale="Because",
            alternatives=["Sessions", "Cookies"],
            category="architecture",
            confidence=5,
        )
        assert result["confidence"] == 5
        assert result["category"] == "architecture"
        assert result["alternatives"] == ["Sessions", "Cookies"]

    def test_list_decisions_auto_detects_session_id(self, tool_server):
        """list_decisions without session_id → auto-detected."""
        server, captured = tool_server
        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="list-session")
        ctx.fastmcp_context = None

        # Capture a decision first
        captured["mempalace_capture_decision"](
            ctx, decision="D1", rationale="R1"
        )

        # List with auto-detected session_id
        result = captured["mempalace_list_decisions"](ctx, session_id=None)
        assert result["count"] == 1
        assert result["decisions"][0]["session_id"] == "list-session"

    def test_wakeup_context_returns_error_when_no_session(self, tool_server):
        """wakeup_context with no session_id available → returns error dict (not raises)."""
        server, captured = tool_server
        ctx = MagicMock(spec=[])
        ctx.request_context = None
        ctx.fastmcp_context = None

        result = captured["mempalace_wakeup_context"](ctx)
        assert "error" in result
        assert "session_id" in result["error"]


# ────────────────────────────────────────────────────────────────────────────
# Test: backward compatibility — session tools remain callable without session_id
# ────────────────────────────────────────────────────────────────────────────

class TestSessionToolsBackwardCompatibility:
    """Verify session tools remain backward-compatible (no new required params)."""

    def test_claim_path_callable_without_session_id(self):
        """claim_path callable with just path — no session_id required."""
        import sys
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._claims_manager = _MockClaimsManager()

        config = MagicMock()
        config.palace_path = tmp

        settings = MagicMock()
        settings.timeout_write = 30
        settings.timeout_read = 15

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="compat-session")
        ctx.fastmcp_context = None

        # Original call pattern: no session_id kwarg
        result = captured["mempalace_claim_path"](ctx, path="/src/main.py")
        assert result.get("owner") == "compat-session"

    def test_release_claim_callable_without_session_id(self):
        """release_claim callable with just path — no session_id required."""
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._claims_manager = _MockClaimsManager()

        config = MagicMock()
        config.palace_path = tmp
        settings = MagicMock()
        settings.timeout_write = 30

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="compat-session")
        ctx.fastmcp_context = None

        # Claim first
        captured["mempalace_claim_path"](ctx, path="/src/main.py", session_id="compat-session")
        # Release without session_id
        result = captured["mempalace_release_claim"](ctx, path="/src/main.py")
        assert result.get("success") is True

    def test_conflict_check_callable_without_session_id(self):
        """conflict_check callable with just path — no session_id required."""
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._claims_manager = _MockClaimsManager()

        config = MagicMock()
        config.palace_path = tmp
        settings = MagicMock()
        settings.timeout_read = 15

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="compat-session")
        ctx.fastmcp_context = None

        result = captured["mempalace_conflict_check"](ctx, path="/src/main.py")
        # Should work (no conflict for unclaimed path)
        assert "has_conflict" in result

    def test_accept_handoff_callable_without_session_id(self):
        """accept_handoff callable with just handoff_id — no session_id required."""
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._handoff_manager = _MockHandoffManager()

        config = MagicMock()
        config.palace_path = tmp
        settings = MagicMock()
        settings.timeout_write = 30

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="compat-session")
        ctx.fastmcp_context = None

        # Push a handoff
        captured["mempalace_push_handoff"](ctx, summary="Test")

        # Accept without session_id
        result = captured["mempalace_accept_handoff"](ctx, handoff_id="h-1")
        assert result.get("success") is True

    def test_complete_handoff_callable_without_session_id(self):
        """complete_handoff callable with just handoff_id — no session_id required."""
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._handoff_manager = _MockHandoffManager()

        config = MagicMock()
        config.palace_path = tmp
        settings = MagicMock()
        settings.timeout_write = 30

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="compat-session")
        ctx.fastmcp_context = None

        captured["mempalace_push_handoff"](ctx, summary="Test")
        captured["mempalace_accept_handoff"](ctx, handoff_id="h-1")
        result = captured["mempalace_complete_handoff"](ctx, handoff_id="h-1")
        assert result.get("success") is True

    def test_capture_decision_callable_without_session_id(self):
        """capture_decision with just decision + rationale — no session_id required."""
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._decision_tracker = _MockDecisionTracker()

        config = MagicMock()
        config.palace_path = tmp
        settings = MagicMock()
        settings.timeout_write = 30

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="compat-session")
        ctx.fastmcp_context = None

        # Minimal call
        result = captured["mempalace_capture_decision"](
            ctx, decision="Test decision", rationale="Because test"
        )
        assert result.get("session_id") == "compat-session"

    def test_list_decisions_callable_without_session_id(self):
        """list_decisions callable with no arguments — no session_id required."""
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        server._decision_tracker = _MockDecisionTracker()

        config = MagicMock()
        config.palace_path = tmp
        settings = MagicMock()
        settings.timeout_read = 15

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = None
        ctx.fastmcp_context = None

        result = captured["mempalace_list_decisions"](ctx)
        assert "decisions" in result

    def test_wakeup_context_callable_with_no_args(self):
        """wakeup_context callable with no arguments — auto-detects everything."""
        from unittest.mock import MagicMock
        from mempalace.server._session_tools import register_session_tools

        tmp = _pp()
        server = MagicMock()
        config = MagicMock()
        config.palace_path = tmp
        settings = MagicMock()
        settings.timeout_read = 15

        captured = {}
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_session_tools(server, MagicMock(), config, settings)

        ctx = MagicMock()
        ctx.request_context = _FakeRequestContext(session_id="no-arg-session")
        ctx.fastmcp_context = None

        # No session_id passed explicitly — tool must not raise TypeError.
        # The call signature must accept ctx alone. We verify this by checking
        # that calling with only ctx doesn't raise TypeError for missing session_id.
        result = captured["mempalace_wakeup_context"](ctx)
        # With session context available, no error about missing session_id
        assert "error" not in result or "session_id" not in str(result.get("error", ""))
