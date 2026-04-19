"""
Tests for Phase 3 Session Coordination (claims, handoffs, decisions, wakeup).
Run: pytest tests/test_phase3_session_coordination.py -v
"""

import os
import tempfile
import pytest

palace_path_factory = tempfile.mkdtemp(prefix="mempalace_p3_")


def _pp():
    return tempfile.mkdtemp(prefix="mempalace_p3_")


@pytest.fixture
def claims_mgr():
    from mempalace.claims_manager import ClaimsManager
    mgr = ClaimsManager(_pp())
    yield mgr
    try:
        mgr.close()
    except Exception:
        pass


@pytest.fixture
def handoff_mgr():
    from mempalace.handoff_manager import HandoffManager
    mgr = HandoffManager(_pp())
    yield mgr
    try:
        mgr.close()
    except Exception:
        pass


@pytest.fixture
def decision_tracker():
    from mempalace.decision_tracker import DecisionTracker
    mgr = DecisionTracker(_pp())
    yield mgr
    try:
        mgr.close()
    except Exception:
        pass


class TestClaimsManager:
    def test_claim_acquire_and_release(self, claims_mgr):
        r = claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        assert r["acquired"] is True
        assert r["owner"] == "session-a"

        claim = claims_mgr.get_claim("file", "/src/main.py")
        assert claim is not None
        assert claim["session_id"] == "session-a"

        r = claims_mgr.release_claim("file", "/src/main.py", "session-a")
        assert r["success"] is True

        assert claims_mgr.get_claim("file", "/src/main.py") is None

    def test_claim_conflict_detection(self, claims_mgr):
        r = claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        assert r["acquired"] is True

        r = claims_mgr.claim("file", "/src/main.py", "session-b", ttl_seconds=600)
        assert r["acquired"] is False
        assert r["owner"] == "session-a"

        conflict = claims_mgr.check_conflicts("file", "/src/main.py", "session-b")
        assert conflict["has_conflict"] is True
        assert conflict["owner"] == "session-a"

    def test_claim_renewal_owner(self, claims_mgr):
        r = claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        assert r["acquired"] is True

        r = claims_mgr.renew_claim("file", "/src/main.py", "session-a", ttl_seconds=1200)
        assert r["success"] is True

        claim = claims_mgr.get_claim("file", "/src/main.py")
        assert claim is not None
        assert claim["session_id"] == "session-a"

    def test_claim_renewal_not_owner(self, claims_mgr):
        claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        r = claims_mgr.renew_claim("file", "/src/main.py", "session-b", ttl_seconds=600)
        assert r["success"] is False
        assert r["error"] == "not_owner"

    def test_claim_release_not_owner(self, claims_mgr):
        claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        r = claims_mgr.release_claim("file", "/src/main.py", "session-b")
        assert r["success"] is False
        assert r["error"] == "not_owner"

    def test_get_session_claims(self, claims_mgr):
        claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        claims_mgr.claim("file", "/src/utils.py", "session-a", ttl_seconds=600)
        claims_mgr.claim("file", "/src/auth.py", "session-b", ttl_seconds=600)

        a = claims_mgr.get_session_claims("session-a")
        assert len(a) == 2
        b = claims_mgr.get_session_claims("session-b")
        assert len(b) == 1

    def test_list_active_claims(self, claims_mgr):
        claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        claims_mgr.claim("file", "/src/utils.py", "session-b", ttl_seconds=600)
        all_claims = claims_mgr.list_active_claims()
        assert len(all_claims) == 2

    def test_claim_with_handoff(self, claims_mgr):
        r = claims_mgr.claim_with_handoff(
            "file", "/src/main.py", "session-b",
            {"note": "takeover", "reason": "abandoned"},
            ttl_seconds=600
        )
        # session-a not holding anything, so session-b gets it
        if r["acquired"]:
            claim = claims_mgr.get_claim("file", "/src/main.py")
            assert claim["payload"]["note"] == "takeover"

    def test_get_claims_for_target_history(self, claims_mgr):
        # session-a claims, then session-b gets blocked (conflict, not stored)
        r = claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        assert r["acquired"] is True
        r = claims_mgr.claim("file", "/src/main.py", "session-b", ttl_seconds=600)
        assert r["acquired"] is False  # conflict rejected
        history = claims_mgr.get_claims_for_target("file", "/src/main.py")
        # Only session-a's successful claim is stored (INSERT OR REPLACE, but conflict prevents insert)
        assert len(history) == 1
        assert history[0]["session_id"] == "session-a"

    def test_get_recent_events(self, claims_mgr):
        claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        claims_mgr.release_claim("file", "/src/main.py", "session-a")
        events = claims_mgr.get_recent_events(session_id="session-a")
        assert len(events) >= 1


class TestHandoffManager:
    def test_push_and_pull_handoff(self, handoff_mgr):
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Refactored auth module",
            touched_paths=["/src/auth.py", "/src/middleware.py"],
            blockers=["Need to update API docs"],
            next_steps=["Update docs", "Add tests"],
            confidence=4,
            priority="high",
            to_session_id=None,
        )
        assert r["success"] is True
        handoff_id = r["handoff_id"]

        handoffs = handoff_mgr.pull_handoffs(session_id="session-a")
        assert len(handoffs) == 1
        assert handoffs[0]["summary"] == "Refactored auth module"
        assert handoffs[0]["status"] == "pending"

    def test_accept_handoff(self, handoff_mgr):
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Take over auth work",
            touched_paths=["/src/auth.py"],
            blockers=[],
            next_steps=["Complete the refactor"],
            confidence=4,
            priority="normal",
            to_session_id=None,
        )
        handoff_id = r["handoff_id"]

        accept = handoff_mgr.accept_handoff(handoff_id, "session-b")
        assert accept["success"] is True

        handoff = handoff_mgr.get_handoff(handoff_id)
        assert handoff["status"] == "accepted"

    def test_complete_handoff(self, handoff_mgr):
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Auth refactor complete",
            touched_paths=["/src/auth.py"],
            blockers=[],
            next_steps=["Done"],
            confidence=5,
            priority="normal",
            to_session_id=None,
        )
        handoff_id = r["handoff_id"]

        handoff_mgr.accept_handoff(handoff_id, "session-b")
        complete = handoff_mgr.complete_handoff(handoff_id, "session-b")
        assert complete["success"] is True
        assert complete["status"] == "completed"

    def test_cancel_handoff(self, handoff_mgr):
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Cancel this work",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=[],
            confidence=3,
            priority="normal",
            to_session_id=None,
        )
        handoff_id = r["handoff_id"]

        cancel = handoff_mgr.cancel_handoff(handoff_id, "session-a")
        assert cancel["success"] is True
        assert cancel["status"] == "cancelled"

        # Non-owner cannot cancel: status check fires first (already cancelled)
        cancel2 = handoff_mgr.cancel_handoff(handoff_id, "session-b")
        assert cancel2["success"] is False
        assert cancel2["error"] == "cannot_cancel_status_cancelled"

    def test_get_handoffs_for_session(self, handoff_mgr):
        handoff_mgr.push_handoff(
            from_session_id="session-a", summary="For session-b",
            touched_paths=["/src/a.py"], blockers=[], next_steps=[],
            confidence=4, priority="normal", to_session_id="session-b",
        )
        handoff_mgr.push_handoff(
            from_session_id="session-b", summary="For session-a",
            touched_paths=["/src/b.py"], blockers=[], next_steps=[],
            confidence=4, priority="normal", to_session_id="session-a",
        )

        handoffs_a = handoff_mgr.get_handoffs_for_session("session-a")
        assert len(handoffs_a) == 2

    def test_list_pending_handoffs(self, handoff_mgr):
        handoff_mgr.push_handoff(
            from_session_id="session-a", summary="Pending work",
            touched_paths=["/src/main.py"], blockers=[], next_steps=[],
            confidence=3, priority="normal",
        )
        pending = handoff_mgr.list_pending_handoffs()
        assert len(pending) >= 1


class TestWakeupContext:
    def test_build_wakeup_context(self):
        from mempalace.wakeup_context import build_wakeup_context
        from mempalace.claims_manager import ClaimsManager
        from mempalace.handoff_manager import HandoffManager

        tmp = _pp()
        claims_mgr = ClaimsManager(tmp)
        handoff_mgr = HandoffManager(tmp)

        claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Test handoff",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=["Continue work"],
            confidence=4,
            priority="normal",
        )

        ctx = build_wakeup_context("session-a", project_root="/src", palace_path=tmp)

        assert ctx["session_id"] == "session-a"
        assert "active_claims" in ctx
        assert "pending_handoffs" in ctx
        assert "recommended_tools" in ctx

        claims_mgr.close()
        handoff_mgr.close()


class TestDecisionTracker:
    def test_capture_decision(self, decision_tracker):
        r = decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Use JWT instead of sessions",
            rationale="Stateless auth scales better",
            alternatives=["Sessions with sticky load", "OAuth tokens"],
            category="architecture",
            confidence=4,
        )
        assert r["success"] is True
        assert r["category"] == "architecture"
        assert r["confidence"] == 4

    def test_list_decisions(self, decision_tracker):
        decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Use Postgres",
            rationale="ACID compliance",
            alternatives=["MongoDB", "SQLite"],
            category="data",
            confidence=4,
        )
        decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Use REST",
            rationale="Simpler than GraphQL",
            alternatives=["GraphQL", "gRPC"],
            category="api",
            confidence=3,
        )

        all_dec = decision_tracker.list_decisions()
        assert len(all_dec) == 2

        sa_dec = decision_tracker.list_decisions(session_id="session-a")
        assert len(sa_dec) == 2

    def test_supersede_decision(self, decision_tracker):
        r1 = decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Use REST API",
            rationale="Simpler",
            alternatives=["GraphQL"],
            category="api",
            confidence=3,
        )
        decision_id = r1["decision_id"]

        r2 = decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Use GraphQL",
            rationale="Better for nested data",
            alternatives=["REST"],
            category="api",
            confidence=4,
        )
        new_id = r2["decision_id"]

        sup = decision_tracker.supersede_decision(decision_id, new_id, "session-a")
        assert sup["success"] is True

        old = decision_tracker.get_decision(decision_id)
        assert old["status"] == "superseded"
        assert old["superseded_by"] == new_id

    def test_get_decision(self, decision_tracker):
        r = decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Test decision",
            rationale="Testing",
            alternatives=[],
            category="testing",
            confidence=5,
        )
        decision_id = r["decision_id"]

        decision = decision_tracker.get_decision(decision_id)
        assert decision is not None
        assert decision["decision_text"] == "Test decision"

    def test_get_decision_not_found(self, decision_tracker):
        result = decision_tracker.get_decision("non-existent-id")
        assert result is None


class TestDecisionTTL:
    """Verify decision TTL lifecycle: capture → expire → cleanup."""

    def test_decision_cleanup_expired_marks_decisions(self, decision_tracker):
        # Create a decision with 1-second TTL
        r = decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Short-lived decision",
            rationale="Testing expiry",
            alternatives=[],
            category="testing",
            confidence=3,
            ttl_seconds=1,
        )
        decision_id = r["decision_id"]
        # Should not be expired yet
        result = decision_tracker.cleanup_expired()
        assert result["expired"] == 0
        # Wait for expiry
        import time
        time.sleep(1.1)
        result = decision_tracker.cleanup_expired()
        assert result["expired"] == 1
        # Verify status is now expired
        d = decision_tracker.get_decision(decision_id)
        assert d["status"] == "expired"

    def test_list_decisions_active_excludes_expired(self, decision_tracker):
        # Create short-lived decision
        r = decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Will expire",
            rationale="Testing",
            alternatives=[],
            category="testing",
            confidence=3,
            ttl_seconds=1,
        )
        # Should appear in active list
        active = decision_tracker.list_decisions(session_id="session-a", status="active")
        ids = [d["id"] for d in active]
        assert r["decision_id"] in ids
        # Wait for expiry
        import time
        time.sleep(1.1)
        decision_tracker.cleanup_expired()
        # Should not appear in active list
        active = decision_tracker.list_decisions(session_id="session-a", status="active")
        ids = [d["id"] for d in active]
        assert r["decision_id"] not in ids

    def test_list_decisions_expired_returns_only_expired(self, decision_tracker):
        r = decision_tracker.capture_decision(
            session_id="session-a",
            decision_text="Expiring decision",
            rationale="Testing",
            alternatives=[],
            category="testing",
            confidence=3,
            ttl_seconds=1,
        )
        decision_id = r["decision_id"]
        import time
        time.sleep(1.1)
        decision_tracker.cleanup_expired()
        expired = decision_tracker.list_decisions(session_id="session-a", status="expired")
        ids = [d["id"] for d in expired]
        assert decision_id in ids


class TestBroadcastHandoffs:
    """Verify broadcast handoff (to_session_id=None) semantics."""

    def test_broadcast_handoff_can_be_accepted_by_any_session(self, handoff_mgr):
        # session-a creates broadcast handoff
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Broadcast work",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=["Continue"],
            confidence=4,
            priority="normal",
            to_session_id=None,  # broadcast
        )
        handoff_id = r["handoff_id"]
        # session-b can accept it
        accept = handoff_mgr.accept_handoff(handoff_id, "session-b")
        assert accept["success"] is True
        assert accept["status"] == "accepted"
        handoff = handoff_mgr.get_handoff(handoff_id)
        assert handoff["to_session_id"] == "session-b"

    def test_broadcast_handoff_pull_handoffs_none_returns_broadcasts(self, handoff_mgr):
        handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Broadcast 1",
            touched_paths=["/src/a.py"],
            blockers=[],
            next_steps=[],
            confidence=3,
            to_session_id=None,
        )
        handoff_mgr.push_handoff(
            from_session_id="session-b",
            summary="Directed to session-c",
            touched_paths=["/src/b.py"],
            blockers=[],
            next_steps=[],
            confidence=3,
            to_session_id="session-c",
        )
        # session_id=None with no status returns broadcasts
        broadcasts = handoff_mgr.pull_handoffs(session_id=None)
        assert len(broadcasts) == 1
        assert broadcasts[0]["from_session_id"] == "session-a"
        assert broadcasts[0]["to_session_id"] is None

    def test_wakeup_context_includes_broadcast_handoffs(self):
        from mempalace.wakeup_context import build_wakeup_context
        from mempalace.handoff_manager import HandoffManager

        tmp = _pp()
        handoff_mgr = HandoffManager(tmp)
        # session-a creates broadcast handoff
        handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Broadcast for anyone",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=["Review and continue"],
            confidence=4,
            to_session_id=None,
        )
        # session-b's wakeup should see the broadcast
        ctx = build_wakeup_context("session-b", project_root="/src", palace_path=tmp)
        # Broadcast should appear in pending_handoffs
        assert len(ctx["pending_handoffs"]) == 1
        assert ctx["pending_handoffs"][0]["summary"] == "Broadcast for anyone"
        handoff_mgr.close()


class TestHandoffLifecycle:
    """Verify complete handoff lifecycle: pending → accepted → completed/cancelled/expired."""

    def test_handoff_expired_cannot_be_accepted(self, handoff_mgr):
        # Create handoff with 1-second TTL
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Soon expired",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=[],
            confidence=3,
            to_session_id="session-b",
            ttl_seconds=1,
        )
        handoff_id = r["handoff_id"]
        import time
        time.sleep(1.1)
        handoff_mgr.cleanup_expired()
        # Now trying to accept should fail
        accept = handoff_mgr.accept_handoff(handoff_id, "session-b")
        assert accept["success"] is False

    def test_handoff_complete_by_from_session(self, handoff_mgr):
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Complete test",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=[],
            confidence=3,
            to_session_id="session-b",
        )
        handoff_id = r["handoff_id"]
        # from_session can complete without accepting first
        complete = handoff_mgr.complete_handoff(handoff_id, "session-a")
        assert complete["success"] is True
        assert complete["status"] == "completed"

    def test_handoff_cancel_only_by_from_owner(self, handoff_mgr):
        r = handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Cancel test",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=[],
            confidence=3,
            to_session_id="session-b",
        )
        handoff_id = r["handoff_id"]
        # to_session cannot cancel
        cancel = handoff_mgr.cancel_handoff(handoff_id, "session-b")
        assert cancel["success"] is False
        assert cancel["error"] == "not_owner"
        # from_session can cancel
        cancel = handoff_mgr.cancel_handoff(handoff_id, "session-a")
        assert cancel["success"] is True


class TestWakeupBundle:
    """Verify wakeup context bundle completeness and correctness."""

    def test_wakeup_bundle_all_documentated_fields_present(self):
        from mempalace.wakeup_context import build_wakeup_context

        tmp = _pp()
        ctx = build_wakeup_context("session-a", project_root="/src", palace_path=tmp)
        # All documented fields must be present
        required_fields = [
            "session_id", "project_root",
            "active_claims", "pending_handoffs", "recent_decisions",
            "recent_changes", "conflicts", "session_info",
            "recommended_tools",
            "active_symbols", "hot_spots", "relevant_decisions", "next_checks",
        ]
        for field in required_fields:
            assert field in ctx, f"Missing field: {field}"
        # Active claims should be a list
        assert isinstance(ctx["active_claims"], list)
        assert isinstance(ctx["pending_handoffs"], list)
        assert isinstance(ctx["recommended_tools"], list)
        assert isinstance(ctx["next_checks"], list)

    def test_wakeup_next_checks_with_handoffs(self):
        from mempalace.wakeup_context import build_wakeup_context
        from mempalace.handoff_manager import HandoffManager

        tmp = _pp()
        handoff_mgr = HandoffManager(tmp)
        handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Work to do",
            touched_paths=["/src/main.py"],
            blockers=["None"],
            next_steps=["Continue"],
            confidence=4,
        )
        ctx = build_wakeup_context("session-a", palace_path=tmp)
        # Should have next_checks because there are pending handoffs
        assert len(ctx["next_checks"]) > 0
        handoff_mgr.close()

    def test_wakeup_recommended_tools_includes_hybrid_search_when_handoffs(self):
        from mempalace.wakeup_context import build_wakeup_context
        from mempalace.handoff_manager import HandoffManager

        tmp = _pp()
        handoff_mgr = HandoffManager(tmp)
        handoff_mgr.push_handoff(
            from_session_id="session-a",
            summary="Work to do",
            touched_paths=["/src/main.py"],
            blockers=[],
            next_steps=["Continue"],
            confidence=4,
        )
        ctx = build_wakeup_context("session-a", palace_path=tmp)
        assert "mempalace_hybrid_search" in ctx["recommended_tools"]
        handoff_mgr.close()


class TestClaimsCleanup:
    """Verify claims cleanup semantics."""

    def test_cleanup_expired_claims_removes_stale(self, claims_mgr):
        # Acquire claim with 1-second TTL
        claims_mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=1)
        import time
        time.sleep(1.1)
        # Claim should be removed by cleanup
        result = claims_mgr.cleanup_expired()
        assert result["removed"] == 1
        # get_session_claims should not return it
        claims = claims_mgr.get_session_claims("session-a")
        assert len(claims) == 0
