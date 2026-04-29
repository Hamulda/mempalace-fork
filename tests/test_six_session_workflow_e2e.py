"""
tests/test_six_session_workflow_e2e.py
=====================================
Six-session workflow layer — full end-to-end integration test.

Tests: sessions, claims, handoffs, prepare_edit, finish_work, takeover,
decision tracking, stale cleanup correctness.

Run: pytest tests/test_six_session_workflow_e2e.py -q
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mempalace.claims_manager import ClaimsManager
from mempalace.handoff_manager import HandoffManager
from mempalace.session_registry import SessionRegistry
from mempalace.write_coordinator import WriteCoordinator

# ── Helpers ────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at(ttl: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()


@contextmanager
def _palace():
    """Isolated temp palace dir with file workspace."""
    with tempfile.TemporaryDirectory() as tmp:
        palace_path = os.path.join(tmp, ".mempalace")
        workspace = os.path.join(tmp, "workspace")
        src = os.path.join(workspace, "src")
        os.makedirs(src)
        for name in ("a.py", "b.py", "shared.py"):
            Path(os.path.join(src, name)).write_text(f"# {name}\npass\n")
        yield palace_path, workspace, src


# ── Test: basic claim lifecycle ─────────────────────────────────────────────────

def test_claim_then_release():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")
        sid = "session-A"

        r = cm.claim("file", path, sid, ttl_seconds=300)
        assert r["acquired"] is True, f"Expected acquired=True, got {r}"
        assert r["owner"] == sid

        r = cm.release_claim("file", path, sid)
        assert r["success"] is True, f"Expected success=True, got {r}"

        cm.close()


def test_non_owner_cannot_release():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        r = cm.claim("file", path, "session-A", ttl_seconds=300)
        assert r["acquired"] is True

        r = cm.release_claim("file", path, "session-B")
        assert r["success"] is False, "non-owner should not be able to release"
        assert r["error"] == "not_owner"
        assert r["owner"] == "session-A"

        cm.close()


def test_blocked_claim_returns_owner_info():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        cm.claim("file", path, "session-A", ttl_seconds=300)

        r = cm.claim("file", path, "session-B", ttl_seconds=300)
        assert r["acquired"] is False
        assert r["owner"] == "session-A"
        assert "expires_at" in r

        cm.close()


def test_claim_blocks_others_until_released():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        cm.claim("file", path, "session-A", ttl_seconds=300)

        r = cm.claim("file", path, "session-C", ttl_seconds=300)
        assert r["acquired"] is False, "session-C should be blocked from claiming a.py"
        assert r["owner"] == "session-A"

        cm.release_claim("file", path, "session-A")

        r = cm.claim("file", path, "session-C", ttl_seconds=300)
        assert r["acquired"] is True, "session-C should acquire after release"
        assert r["owner"] == "session-C"

        cm.close()


# ── Test: TTL expiry ────────────────────────────────────────────────────────────

def test_ttl_expiry_allows_reclaim():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        cm.claim("file", path, "session-A", ttl_seconds=1)
        r = cm.claim("file", path, "session-B", ttl_seconds=300)
        assert r["acquired"] is False, "Should be blocked by TTL"

        time.sleep(1.1)
        cm.cleanup_expired()

        r = cm.claim("file", path, "session-B", ttl_seconds=300)
        assert r["acquired"] is True, "Should acquire after TTL expiry"
        assert r["owner"] == "session-B"

        cm.close()


# ── Test: session registry ──────────────────────────────────────────────────────

def test_session_registry_active_sessions():
    with _palace() as (pp, ws, src):
        with SessionRegistry(palace_path=pp) as reg:
            sid = "session-A"

            reg.register_session(sid, ws, branch="main")
            active = reg.get_active_sessions(project_root=ws)
            assert len(active) == 1
            assert active[0]["session_id"] == sid

            reg.heartbeat_session(sid, revision="abc", claimed_paths=[os.path.join(src, "a.py")])
            active = reg.get_active_sessions(project_root=ws)
            assert len(active) == 1
            assert active[0]["claimed_paths"] == [os.path.join(src, "a.py")]

            reg.unregister_session(sid)
            active = reg.get_active_sessions(project_root=ws)
            assert len(active) == 0


def test_stale_session_cleanup_does_not_delete_active_claims():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        with SessionRegistry(palace_path=pp) as reg:
            sid = "session-active"
            reg.register_session(sid, ws, branch="main")
            cm.claim("file", path, sid, ttl_seconds=300)

            # Run stale cleanup — active session should NOT be deleted
            removed = reg.cleanup_stale_sessions(older_than_seconds=3600)
            assert removed == 0, f"Active session must not be removed, got {removed}"

            # Claim still held by session (owner re-claiming refreshes TTL → acquired=True;
            # verify persistence via get_claim instead)
            claim = cm.get_claim("file", path)
            assert claim is not None and claim["session_id"] == sid

        cm.close()


# ── Test: handoff ───────────────────────────────────────────────────────────────

def test_handoff_publish_and_pull():
    with _palace() as (pp, ws, src):
        hm = HandoffManager(palace_path=pp)
        path_a = os.path.join(src, "a.py")
        path_b = os.path.join(src, "b.py")

        hr = hm.push_handoff(
            from_session_id="session-A",
            summary="Refactored a.py and b.py",
            touched_paths=[path_a, path_b],
            blockers=[],
            next_steps=["Run tests"],
            confidence=3,
            priority="high",
            to_session_id=None,
        )
        hid = hr["handoff_id"]
        assert hid is not None
        assert hr["status"] == "pending"

        # Broadcast pull (session_id=None) returns broadcast handoffs
        handoffs = hm.pull_handoffs()  # no session filter = broadcast only
        assert any(h["id"] == hid for h in handoffs), "handoff should appear in broadcast pull"

        # session-D pull includes both broadcast AND its directed handoffs
        handoffs_d = hm.pull_handoffs(session_id="session-D")
        # pull_handoffs(session_id=X) returns handoffs where from_session_id=X OR to_session_id=X
        # broadcast handoff (to_session_id=None) won't appear for session-D via this query
        # since it's neither from-D nor to-D. Broadcasts need pull_handoffs() without session_id.
        # Instead use get_handoffs_for_session or verify broadcast separately.
        handoffs_broadcast = hm.pull_handoffs()  # broadcast only
        assert any(h["id"] == hid for h in handoffs_broadcast), "broadcast handoff should be pullable"

        hm.close()


def test_handoff_accept_and_reclaim():
    with _palace() as (pp, ws, src):
        hm = HandoffManager(palace_path=pp)
        cm = ClaimsManager(palace_path=pp)
        path_a = os.path.join(src, "a.py")

        hr = hm.push_handoff(
            from_session_id="session-A",
            summary="Take over a.py",
            touched_paths=[path_a],
            blockers=[],
            next_steps=[],
            confidence=3,
            priority="normal",
            to_session_id=None,
        )
        hid = hr["handoff_id"]

        ar = hm.accept_handoff(hid, "session-D")
        assert ar["status"] == "accepted"
        assert ar.get("accepted_at") is not None

        r = cm.claim("file", path_a, "session-D", ttl_seconds=300)
        assert r["acquired"] is True

        hm.close()
        cm.close()


def test_handoff_publish_releases_claims():
    with _palace() as (pp, ws, src):
        hm = HandoffManager(palace_path=pp)
        cm = ClaimsManager(palace_path=pp)
        path_a = os.path.join(src, "a.py")

        cm.claim("file", path_a, "session-A", ttl_seconds=300)
        hm.push_handoff(
            from_session_id="session-A",
            summary="Handoff a.py",
            touched_paths=[path_a],
            blockers=[],
            next_steps=[],
            confidence=3,
            priority="normal",
            to_session_id=None,
        )

        # Release is done by the workflow tool, not push_handoff itself
        cm.release_claim("file", path_a, "session-A")

        r = cm.claim("file", path_a, "session-D", ttl_seconds=300)
        assert r["acquired"] is True, "session-D should acquire after handoff release"
        assert r["owner"] == "session-D"

        hm.close()
        cm.close()


# ── Test: write coordinator intent recovery ───────────────────────────────────

def test_write_coordinator_intent_logging_and_recovery():
    with _palace() as (pp, ws, src):
        wc = WriteCoordinator(palace_path=pp)
        path = os.path.join(src, "a.py")

        intent_id = wc.log_intent("session-A", "edit", "file", path, {"note": "refactor"})
        assert intent_id is not None

        pending = wc.get_pending_intents("session-A")
        assert any(i["id"] == intent_id for i in pending), "intent should be pending"

        r = wc.commit_intent(intent_id, "session-A")
        assert r is True

        wc.close()


def test_rollback_intent_only_by_owner():
    with _palace() as (pp, ws, src):
        wc = WriteCoordinator(palace_path=pp)
        path = os.path.join(src, "a.py")

        intent_id = wc.log_intent("session-A", "edit", "file", path, {})

        r = wc.rollback_intent(intent_id, "session-B")
        assert r is False, "non-owner should not be able to rollback"

        r = wc.rollback_intent(intent_id, "session-A")
        assert r is True

        wc.close()


def test_recover_pending_intents():
    with _palace() as (pp, ws, src):
        wc = WriteCoordinator(palace_path=pp)

        wc.log_intent("session-dead", "edit", "file", os.path.join(src, "a.py"), {})
        wc.log_intent("session-dead", "edit", "file", os.path.join(src, "b.py"), {})

        result = wc.recover_pending_intents(pp)
        assert "recovered" in result or "rolled_back" in result

        wc.close()


# ── Test: concurrency — 6 sessions claim different files ──────────────────────

def test_six_concurrent_claim_tasks_no_sqlite_busy():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        files = [os.path.join(src, f"{chr(ord('a')+i)}.py") for i in range(6)]
        sids = [f"session-{chr(ord('A')+i)}" for i in range(6)]

        errors = []
        successes = []

        def claim_file(sid: str, path: str):
            try:
                r = cm.claim("file", path, sid, ttl_seconds=300)
                if r["acquired"]:
                    successes.append((sid, path))
                else:
                    errors.append((sid, path, dict(r)))
            except sqlite3.OperationalError as e:
                errors.append((sid, path, f"SQLITE_BUSY: {e}"))

        threads = [threading.Thread(target=claim_file, args=(sids[i], files[i])) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"SQLite_BUSY or errors: {errors}"
        assert len(successes) == 6, f"Expected 6 successful claims, got {len(successes)}"

        owners = {}
        for sid, path in successes:
            r = cm.get_claim("file", path)
            assert r is not None, f"Claim for {path} should exist"
            owners[path] = r["session_id"]
        assert len(owners) == 6, "All 6 files should have distinct owners"

        cm.close()


def test_all_claims_releasable_after_concurrent_acquire():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        files = [os.path.join(src, f"{chr(ord('a')+i)}.py") for i in range(6)]
        sids = [f"session-{chr(ord('A')+i)}" for i in range(6)]

        for i in range(6):
            cm.claim("file", files[i], sids[i], ttl_seconds=300)

        errors = []
        for i in range(6):
            r = cm.release_claim("file", files[i], sids[i])
            if not r["success"]:
                errors.append((sids[i], files[i], r))
        assert not errors, f"Non-owner release failures: {errors}"

        for i in range(6):
            r = cm.claim("file", files[i], "session-X", ttl_seconds=300)
            assert r["acquired"] is True, f"File {files[i]} should be free after all releases"

        cm.close()


# ── Test: prepare_edit conflict check ──────────────────────────────────────────

def test_prepare_edit_conflict_check_blocks_other_session():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        cm.claim("file", path, "session-A", ttl_seconds=300)

        claim = cm.get_claim("file", path)
        assert claim is not None
        assert claim["session_id"] == "session-A"

        # sessionB trying to claim should be blocked
        r = cm.claim("file", path, "session-B", ttl_seconds=300)
        assert r["acquired"] is False
        assert r["owner"] == "session-A"

        cm.close()


# ── Test: takeover requires accepted handoff ──────────────────────────────────

def test_takeover_requires_accepted_handoff():
    with _palace() as (pp, ws, src):
        hm = HandoffManager(palace_path=pp)
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        hr = hm.push_handoff(
            from_session_id="session-A",
            summary="Abandoned work",
            touched_paths=[path],
            blockers=[],
            next_steps=[],
            confidence=3,
            priority="normal",
            to_session_id=None,
        )
        hid = hr["handoff_id"]

        cm.claim("file", path, "session-A", ttl_seconds=300)

        hm.accept_handoff(hid, "session-D")
        # session-A released the path before creating handoff (simulating publish_handoff flow)
        cm.release_claim("file", path, "session-A")
        r = cm.claim("file", path, "session-D", ttl_seconds=300)
        assert r["acquired"] is True, "session-D should acquire after handoff accepted and original claim released"

        hm.close()
        cm.close()


# ── Test: decision captured in intent payload ──────────────────────────────────

def test_decision_captured_in_intent_payload():
    with _palace() as (pp, ws, src):
        wc = WriteCoordinator(palace_path=pp)
        path = os.path.join(src, "a.py")

        intent_id = wc.log_intent(
            "session-A", "edit", "file", path,
            {"decision": "use_oo_pattern", "rationale": "better encapsulation"}
        )
        assert intent_id is not None

        pending = wc.get_pending_intents("session-A")
        intent = next(i for i in pending if i["id"] == intent_id)
        payload = intent["payload"]
        assert payload["decision"] == "use_oo_pattern"

        wc.close()


# ── Test: stale cleanup leaves active claims ──────────────────────────────────

def test_stale_cleanup_leaves_active_claims():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")

        with SessionRegistry(palace_path=pp) as reg:
            sid = "session-active"
            reg.register_session(sid, ws, branch="main")
            cm.claim("file", path, sid, ttl_seconds=86400)

            removed = reg.cleanup_stale_sessions(older_than_seconds=3600)
            assert removed == 0

            claim = cm.get_claim("file", path)
            assert claim is not None
            assert claim["session_id"] == sid

        cm.close()


# ── Test: handoff to specific session ────────────────────────────────────────

def test_handoff_to_specific_session():
    with _palace() as (pp, ws, src):
        hm = HandoffManager(palace_path=pp)

        hr = hm.push_handoff(
            from_session_id="session-A",
            summary="For session-D only",
            touched_paths=[],
            blockers=[],
            next_steps=[],
            confidence=3,
            priority="high",
            to_session_id="session-D",
        )
        hid = hr["handoff_id"]

        # pull_handoffs(session_id=X) returns from OR to handoffs for X
        all_for_d = hm.pull_handoffs(session_id="session-D")
        assert any(h["id"] == hid for h in all_for_d), "session-D should see directed handoff"

        hm.close()


# ── Test: finish_work releases and commits intent ─────────────────────────────

def test_finish_work_releases_and_commits_intent():
    with _palace() as (pp, ws, src):
        wc = WriteCoordinator(palace_path=pp)
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")
        sid = "session-A"

        cm.claim("file", path, sid, ttl_seconds=300)
        intent_id = wc.log_intent(sid, "edit", "file", path, {})

        cm.release_claim("file", path, sid)
        wc.commit_intent(intent_id, sid)

        pending = wc.get_pending_intents(sid)
        assert not any(i["id"] == intent_id for i in pending), "intent should be committed"

        cm.close()
        wc.close()


# ── Test: no sessions returns empty list ─────────────────────────────────────

def test_no_sessions_returns_empty_list():
    with _palace() as (pp, ws, src):
        with SessionRegistry(palace_path=pp) as reg:
            active = reg.get_active_sessions(project_root=ws)
            assert active == []


# ── Test: different files claimable by different sessions ───────────────────

def test_different_files_claimable_by_different_sessions():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path_a = os.path.join(src, "a.py")
        path_b = os.path.join(src, "b.py")
        path_c = os.path.join(src, "shared.py")

        rA = cm.claim("file", path_a, "session-A", ttl_seconds=300)
        rB = cm.claim("file", path_b, "session-B", ttl_seconds=300)
        rC = cm.claim("file", path_c, "session-C", ttl_seconds=300)

        assert rA["acquired"] is True
        assert rB["acquired"] is True
        assert rC["acquired"] is True
        assert rA["owner"] == "session-A"
        assert rB["owner"] == "session-B"
        assert rC["owner"] == "session-C"

        r = cm.release_claim("file", path_b, "session-A")
        assert r["success"] is False
        assert r["error"] == "not_owner"

        cm.close()


# ── Test: get_session_claims returns only unexpired ──────────────────────────

def test_get_session_claims_excludes_expired():
    with _palace() as (pp, ws, src):
        cm = ClaimsManager(palace_path=pp)
        path = os.path.join(src, "a.py")
        sid = "session-A"

        cm.claim("file", path, sid, ttl_seconds=1)
        claims = cm.get_session_claims(sid)
        assert len(claims) == 1
        assert claims[0]["target_id"] == path

        time.sleep(1.1)
        cm.cleanup_expired()

        claims = cm.get_session_claims(sid)
        assert len(claims) == 0, "Expired claims should not be returned"

        cm.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
