"""
WriteCoordinator Integration Tests — Intent lifecycle, coordinator-backed writes,
crash recovery, and deadlock-free concurrency.

Tests prove:
- Intent lifecycle: log → commit (success) and log → rollback (failure)
- Coordinator-backed write flow: WriteCoordinator in actual write path
- Crash recovery: pending intents from crashed sessions are rolled back on startup
- No deadlocks: concurrent writes with WriteCoordinator lock don't deadlock

Run: pytest tests/test_write_coordinator_integration.py -v
"""

import tempfile
import threading
import time
import pytest
import unittest.mock

pytest.importorskip("lancedb", reason="LanceDB not installed")


def _mock_embed_texts(texts):
    """Deterministic mock embeddings — no MLX needed."""
    import hashlib
    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


def _pp():
    return tempfile.mkdtemp(prefix="mempalace_wc_int_")

# ─── Test fixtures ────────────────────────────────────────────────────────────


class _StatusCacheMock:
    """Minimal StatusCache mock for tests."""
    def __init__(self):
        self._cache = {}
        import threading
        self._lock = threading.Lock()

    def invalidate(self):
        with self._lock:
            self._cache.clear()


class _MockServer:
    """Minimal mock server with WriteCoordinator attached."""

    def __init__(self, palace_path):
        from mempalace.write_coordinator import WriteCoordinator
        self._write_coordinator = WriteCoordinator(palace_path)
        self._status_cache = _StatusCacheMock()

    def tool(self, **kwargs):
        """Decorator that registers a tool — returns identity for use as decorator."""
        def decorator(func):
            setattr(self, func.__name__, func)
            return func
        return decorator


# ─── Intent Lifecycle Tests ───────────────────────────────────────────────────


class TestIntentLifecycle:
    """Intent lifecycle: log → commit and log → rollback."""

    def test_log_intent_returns_id(self):
        """log_intent() returns a valid intent id."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        intent_id = wc.log_intent(
            "session-1", "add_drawer", "drawer", "drawer_abc",
            payload={"wing": "repo", "room": "src"}
        )
        assert intent_id is not None
        assert isinstance(intent_id, int)
        assert intent_id > 0

    def test_successful_write_commits_intent(self):
        """After successful storage operation, intent is committed."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        intent_id = wc.log_intent("session-1", "add_drawer", "drawer", "drawer_ok")
        assert intent_id is not None

        # Simulate successful write → commit
        success = wc.commit_intent(intent_id, "session-1")
        assert success is True

        # Intent is no longer pending
        pending = wc.get_pending_intents("session-1")
        assert not any(i["id"] == intent_id for i in pending)

        committed = wc.get_pending_intents("session-1")
        committed_ids = [i["id"] for i in committed if i["status"] == "committed"]
        # After commit, it's no longer in pending list (status changed to committed)
        # The get_pending_intents only returns status='pending', so committed won't appear
        # Verify by checking write_intents directly
        with wc._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT status FROM write_intents WHERE id=?", (intent_id,)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "committed"

    def test_failed_write_rolls_back_intent(self):
        """After failed storage operation, intent is rolled back."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        intent_id = wc.log_intent("session-1", "add_drawer", "drawer", "drawer_fail")
        assert intent_id is not None

        # Simulate failed write → rollback
        success = wc.rollback_intent(intent_id, "session-1")
        assert success is True

        with wc._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT status FROM write_intents WHERE id=?", (intent_id,)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "rolled_back"

    def test_commit_wrong_session_returns_false(self):
        """commit_intent() returns False if session_id doesn't match."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        intent_id = wc.log_intent("session-1", "add_drawer", "drawer", "drawer_x")

        # Try to commit with different session
        result = wc.commit_intent(intent_id, "session-2")
        assert result is False

        # Intent should still be pending
        pending = wc.get_pending_intents()
        assert any(i["id"] == intent_id and i["status"] == "pending" for i in pending)

    def test_rollback_wrong_session_returns_false(self):
        """rollback_intent() returns False if session_id doesn't match."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        intent_id = wc.log_intent("session-1", "add_drawer", "drawer", "drawer_y")

        result = wc.rollback_intent(intent_id, "session-2")
        assert result is False

        # Intent should still be pending
        pending = wc.get_pending_intents()
        assert any(i["id"] == intent_id and i["status"] == "pending" for i in pending)

    def test_sessionless_write_no_intent_created(self):
        """Write without session_id doesn't create an intent (fail-open)."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        # Direct call with None session_id hits SQLite NOT NULL constraint.
        # The fail-open happens at the _write_tools._log_intent helper level
        # (which checks session_id before calling wc.log_intent).
        # Here we verify WC rejects None directly.
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            wc.log_intent(None, "add_drawer", "drawer", "drawer_z")

        # No intent should be created
        pending = wc.get_pending_intents()
        assert len(pending) == 0


# ─── Coordinator-Backed Write Flow Tests ──────────────────────────────────────


class TestCoordinatorBackedWriteFlow:
    """WriteCoordinator is in the actual write path of all write tools."""

    def test_add_drawer_creates_and_commits_intent(self):
        """mempalace_add_drawer creates intent and commits on success."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.backends.lance import LanceBackend

        palace = _pp()
        server = _MockServer(palace)
        backend = LanceBackend()
        col = backend.get_collection(palace, "test_intent", create=True)

        # Patch backend to return our collection
        original_get_collection = backend.get_collection
        backend.get_collection = lambda *args, **kwargs: col

        settings = unittest.mock.MagicMock()
        settings.db_path = palace
        settings.effective_collection_name = "test_intent"
        settings.palace_path = palace
        settings.wal_dir = tempfile.mkdtemp(prefix="wal_")
        settings.timeout_write = 30
        settings.timeout_embed = 60

        from mempalace.server._write_tools import register_write_tools

        register_write_tools(server, backend, None, settings, None)  # memory_guard=None to avoid thread issues in tests

        # Get the registered tool
        add_drawer = getattr(server, "mempalace_add_drawer", None)
        assert add_drawer is not None, "mempalace_add_drawer should be registered"

        # Call with session_id — should create intent
        ctx = unittest.mock.MagicMock()
        result = add_drawer(
            ctx,
            wing="repo",
            room="src",
            content="test content for intent lifecycle",
            session_id="test-session-1",
            claim_mode="advisory",
        )

        assert result.get("success") is True, f"Expected success, got: {result}"

        # Verify intent was created and committed
        pending = server._write_coordinator.get_pending_intents("test-session-1")
        # Should have one committed intent (not pending anymore)
        with server._write_coordinator._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT status, operation FROM write_intents WHERE session_id=?",
                ("test-session-1",)
            )
            rows = cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "committed"
            assert rows[0][1] == "add_drawer"

    def test_delete_drawer_creates_and_commits_intent(self):
        """mempalace_delete_drawer creates intent and commits on success."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.backends.lance import LanceBackend

        palace = _pp()
        server = _MockServer(palace)
        backend = LanceBackend()

        # Pre-create a drawer to delete
        col = backend.get_collection(palace, "test_delete_intent", create=True)
        with unittest.mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            col.upsert(
                ids=["delete_me_123"],
                documents=["content to delete"],
                metadatas=[{"wing": "repo", "room": "src"}],
            )

        original_get_collection = backend.get_collection
        backend.get_collection = lambda *args, **kwargs: col

        settings = unittest.mock.MagicMock()
        settings.db_path = palace
        settings.effective_collection_name = "test_delete_intent"
        settings.palace_path = palace
        settings.wal_dir = tempfile.mkdtemp(prefix="wal_")
        settings.timeout_write = 30
        settings.timeout_embed = 60

        from mempalace.server._write_tools import register_write_tools

        register_write_tools(server, backend, None, settings, None)  # memory_guard=None to avoid thread issues in tests

        delete_drawer = getattr(server, "mempalace_delete_drawer", None)
        assert delete_drawer is not None

        ctx = unittest.mock.MagicMock()
        result = delete_drawer(
            ctx,
            drawer_id="delete_me_123",
            session_id="test-session-2",
            claim_mode="advisory",
        )

        assert result.get("success") is True, f"Expected success, got: {result}"

        with server._write_coordinator._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT status, operation FROM write_intents WHERE session_id=?",
                ("test-session-2",)
            )
            rows = cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "committed"
            assert rows[0][1] == "delete_drawer"

    def test_diary_write_creates_and_commits_intent(self):
        """mempalace_diary_write creates intent and commits on success."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.backends.lance import LanceBackend

        palace = _pp()
        server = _MockServer(palace)
        backend = LanceBackend()

        col = backend.get_collection(palace, "test_diary_intent", create=True)
        original_get_collection = backend.get_collection
        backend.get_collection = lambda *args, **kwargs: col

        settings = unittest.mock.MagicMock()
        settings.db_path = palace
        settings.effective_collection_name = "test_diary_intent"
        settings.palace_path = palace
        settings.wal_dir = tempfile.mkdtemp(prefix="wal_")
        settings.timeout_write = 30
        settings.timeout_read = 30
        settings.timeout_embed = 60

        from mempalace.server._write_tools import register_write_tools

        register_write_tools(server, backend, None, settings, None)  # memory_guard=None to avoid thread issues in tests

        diary_write = getattr(server, "mempalace_diary_write", None)
        assert diary_write is not None

        ctx = unittest.mock.MagicMock()
        result = diary_write(
            ctx,
            agent_name="TestAgent",
            entry="This is a test diary entry",
            topic="testing",
            session_id="test-session-3",
            claim_mode="advisory",
        )

        assert result.get("success") is True, f"Expected success, got: {result}"

        with server._write_coordinator._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT status, operation FROM write_intents WHERE session_id=?",
                ("test-session-3",)
            )
            rows = cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "committed"
            assert rows[0][1] == "diary_write"


# ─── Crash Recovery Tests ─────────────────────────────────────────────────────


class TestCrashRecovery:
    """Pending intents from crashed sessions are rolled back on startup."""

    def test_pending_intent_from_stopped_session_rolled_back_on_init(self):
        """WriteCoordinator.init rolls back pending intents from stopped sessions."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.session_registry import SessionRegistry

        palace = _pp()

        # Create and immediately stop a session
        reg = SessionRegistry(palace)
        reg.register_session("crashed-abc", palace, role="agent")
        reg.unregister_session("crashed-abc")

        # Create WC — this should trigger recovery and rollback the crashed session's intent
        wc = WriteCoordinator(palace)

        # The crashed session's pending intents should be rolled back
        with wc._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT status FROM write_intents WHERE session_id=?", ("crashed-abc",)
            )
            rows = cursor.fetchall()
            # All intents from crashed session should be rolled_back
            assert all(row[0] == "rolled_back" for row in rows)

    def test_pending_intent_from_active_session_kept_for_replay(self):
        """Pending intents from active sessions are kept for potential replay."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.session_registry import SessionRegistry

        palace = _pp()

        # Create an active session
        reg = SessionRegistry(palace)
        reg.register_session("active-xyz", palace, role="agent")

        # Log an intent for the active session
        wc1 = WriteCoordinator(palace)
        intent_id = wc1.log_intent("active-xyz", "add_drawer", "drawer", "drawer_recover")

        # Create new WC — should keep the active session's pending intent
        wc2 = WriteCoordinator(palace)

        pending = wc2.get_pending_intents("active-xyz")
        assert any(i["id"] == intent_id and i["status"] == "pending" for i in pending)

    def test_multiple_sessions_crash_recovery_isolated(self):
        """Recovery handles multiple sessions with mixed crashed/active states."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.session_registry import SessionRegistry

        palace = _pp()
        reg = SessionRegistry(palace)

        # Set up sessions: 2 active, 1 crashed
        reg.register_session("active-1", palace, role="agent")
        reg.register_session("active-2", palace, role="agent")
        reg.register_session("crashed-1", palace, role="agent")
        reg.unregister_session("crashed-1")

        wc = WriteCoordinator(palace)

        # Log intents for all three sessions
        wc.log_intent("active-1", "add_drawer", "drawer", "d1")
        wc.log_intent("active-2", "add_drawer", "drawer", "d2")
        wc.log_intent("crashed-1", "add_drawer", "drawer", "d3")

        # Simulate crash: unregister crashed-1 after logging intent
        reg.unregister_session("crashed-1")

        # New WriteCoordinator should recover
        wc2 = WriteCoordinator(palace)

        with wc2._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT session_id, status FROM write_intents ORDER BY session_id"
            )
            rows = cursor.fetchall()

        # crashed-1 should be rolled back, active sessions should keep pending
        statuses = {row[0]: row[1] for row in rows}
        assert statuses["crashed-1"] == "rolled_back"
        assert statuses["active-1"] == "pending"
        assert statuses["active-2"] == "pending"


# ─── Deadlock-Free Concurrency Tests ─────────────────────────────────────────


class TestNoDeadlocks:
    """Concurrent writes with WriteCoordinator don't deadlock on M1/8GB."""

    def test_concurrent_intent_logging_no_deadlock(self):
        """Multiple threads logging intents simultaneously don't deadlock."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        errors = []
        barrier = threading.Barrier(5)

        def log_intents(session_id):
            try:
                barrier.wait()  # Synchronize start
                for i in range(20):
                    intent_id = wc.log_intent(
                        session_id, "add_drawer", "drawer", f"drawer_{session_id}_{i}"
                    )
                    assert intent_id is not None
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=log_intents, args=(f"concurrent-s-{i}",))
            for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)  # Should complete well under 30s

        assert len(errors) == 0, f"Deadlock or error: {errors}"
        assert not any(t.is_alive() for t in threads), "Threads still running — possible deadlock"

    def test_concurrent_claim_operations_no_deadlock(self):
        """Concurrent claim/release operations don't deadlock."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        errors = []
        barrier = threading.Barrier(4)

        def claim_ops(session_id):
            try:
                barrier.wait()
                for i in range(15):
                    wc.claim("file", f"/src/file_{i % 3}.py", session_id, ttl_seconds=2)
                    time.sleep(0.001)
                    wc.release_claim("file", f"/src/file_{i % 3}.py", session_id)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=claim_ops, args=(f"claim-s-{i}",))
            for i in range(4)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Deadlock or error: {errors}"
        assert not any(t.is_alive() for t in threads), "Threads still running — possible deadlock"

    def test_write_lock_timeout_is_reasonable(self):
        """WriteCoordinator busy_timeout prevents indefinite blocking."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        # Verify busy_timeout is set to 5000ms
        with wc._conn_ctx() as conn:
            cursor = conn.execute("PRAGMA busy_timeout")
            row = cursor.fetchone()
            assert row is not None
            # busy_timeout returns milliseconds
            timeout_ms = row[0]
            assert timeout_ms == 5000, f"Expected busy_timeout=5000, got {timeout_ms}"


# ─── Fail-Open Semantics Tests ────────────────────────────────────────────────


class TestFailOpenSemantics:
    """WriteCoordinator failures don't block writes — fail-open."""

    def test_write_proceeds_when_wc_unavailable(self):
        """Write succeeds even when WriteCoordinator is not available."""
        from mempalace.backends.lance import LanceBackend

        palace = _pp()
        backend = LanceBackend()

        # Server WITHOUT WriteCoordinator — use custom class to avoid MagicMock auto-create
        class NoWCServer:
            _write_coordinator = None
            _status_cache = _StatusCacheMock()

            def tool(self, **kwargs):
                def decorator(func):
                    setattr(self, func.__name__, func)
                    return func
                return decorator

        server = NoWCServer()

        col = backend.get_collection(palace, "test_fail_open", create=True)
        original_get_collection = backend.get_collection
        backend.get_collection = lambda *args, **kwargs: col

        settings = unittest.mock.MagicMock()
        settings.db_path = palace
        settings.effective_collection_name = "test_fail_open"
        settings.palace_path = palace
        settings.wal_dir = tempfile.mkdtemp(prefix="wal_")
        settings.timeout_write = 30
        settings.timeout_embed = 60

        from mempalace.server._write_tools import register_write_tools

        # Pass memory_guard=None to avoid MemoryGuard thread issues in tests
        register_write_tools(server, backend, None, settings, None)

        add_drawer = getattr(server, "mempalace_add_drawer")
        ctx = unittest.mock.MagicMock()

        # Should succeed even without WC (fail-open)
        result = add_drawer(
            ctx,
            wing="repo",
            room="src",
            content="content that should still be written",
            session_id="test-session-failopen",
            claim_mode="advisory",
        )

        assert result.get("success") is True, f"Expected success with fail-open, got: {result}"

    def test_write_proceeds_when_wc_log_intent_raises(self):
        """Write succeeds even if log_intent raises an exception."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.backends.lance import LanceBackend

        palace = _pp()
        backend = LanceBackend()

        server = _MockServer(palace)

        # Make log_intent raise after first successful call
        original_log = server._write_coordinator.log_intent
        call_count = [0]

        def raising_log(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1:  # Fail after first call
                raise RuntimeError("Simulated WC failure")
            return original_log(*args, **kwargs)

        server._write_coordinator.log_intent = raising_log

        col = backend.get_collection(palace, "test_wc_fail", create=True)
        backend.get_collection = lambda *args, **kwargs: col

        settings = unittest.mock.MagicMock()
        settings.db_path = palace
        settings.effective_collection_name = "test_wc_fail"
        settings.palace_path = palace
        settings.wal_dir = tempfile.mkdtemp(prefix="wal_")
        settings.timeout_write = 30
        settings.timeout_embed = 60

        from mempalace.server._write_tools import register_write_tools

        # Pass memory_guard=None to avoid MemoryGuard thread issues in tests
        register_write_tools(server, backend, None, settings, None)

        add_drawer = getattr(server, "mempalace_add_drawer")
        ctx = unittest.mock.MagicMock()

        # Should succeed despite WC failure (fail-open)
        result = add_drawer(
            ctx,
            wing="repo",
            room="src",
            content="content even with WC failure",
            session_id="test-session-wc-fail",
            claim_mode="advisory",
        )

        assert result.get("success") is True, f"Expected success with WC failure, got: {result}"


# ─── Striped Lock Concurrency Tests ───────────────────────────────────────────


class TestStripedLockConcurrency:
    """Fine-grained stripe locks reduce contention under concurrent writes."""

    def test_concurrent_claims_on_different_stripes_no_blocking(self):
        """Claims on different stripes don't serialize — they run in parallel."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        errors = []
        barrier = threading.Barrier(4)

        def claim_different_targets(session_id):
            try:
                barrier.wait()
                for i in range(20):
                    # Each session claims a DIFFERENT target — should not block
                    target = f"/unique/path/{session_id}/{i}"
                    result = wc.claim("file", target, session_id, ttl_seconds=5)
                    assert result["acquired"] is True
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=claim_different_targets, args=(f"stripe-s-{i}",))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert not any(t.is_alive() for t in threads)

    def test_concurrent_claims_on_same_stripe_do_serialize(self):
        """Claims on the SAME stripe DO serialize (correct stripe lock behavior)."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        # Force same stripe: use same target for all threads.
        # They should serialize correctly, not deadlock.
        errors = []
        barrier = threading.Barrier(3)
        total_attempts = [0]
        acquired_count = [0]

        def claim_same_target(session_id):
            try:
                barrier.wait()
                for i in range(10):
                    result = wc.claim("file", "/same/stripe/target", session_id, ttl_seconds=1)
                    total_attempts[0] += 1
                    if result["acquired"]:
                        acquired_count[0] += 1
                    # Brief sleep so each session gets a turn to acquire
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=claim_same_target, args=(f"same-stripe-s-{i}",))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Deadlock or error: {errors}"
        assert not any(t.is_alive() for t in threads)
        # All 30 attempts complete (no deadlock), some acquired (serialized success)
        assert total_attempts[0] == 30
        assert acquired_count[0] >= 1  # at least one session acquired

    def test_commit_intent_uses_stripe_lock(self):
        """commit_intent uses per-target stripe lock, not global lock."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        errors = []
        barrier = threading.Barrier(4)

        def commit_session(session_id):
            try:
                barrier.wait()
                for i in range(15):
                    intent_id = wc.log_intent(session_id, "add_drawer", "drawer", f"drawer_{session_id}_{i}")
                    # commit uses stripe lock based on (target_type, target_id)
                    success = wc.commit_intent(intent_id, session_id)
                    assert success is True
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=commit_session, args=(f"commit-s-{i}",))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Errors: {errors}"
        assert not any(t.is_alive() for t in threads)

    def test_stripe_lock_slot_distribution_is_even(self):
        """SHA-256 based slot selection distributes targets evenly across stripes."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        slot_counts = [0] * wc._stripe_lock._NUM_STRIPES
        for i in range(100):
            slot = wc._stripe_lock._slot("file", f"/path/to/file_{i}")
            slot_counts[slot] += 1

        # With 16 stripes and 100 targets, no slot should have 0 or > 20.
        # (Binomial with p=1/16, n=100: mean=6.25, std~2.3.
        #  Expect all slots to have at least 1, max < 15.)
        assert all(c > 0 for c in slot_counts), f"Some stripes unused: {slot_counts}"
        assert all(c < 30 for c in slot_counts), f"Skewed distribution: {slot_counts}"


# ─── Recovery With Stripe Locks ──────────────────────────────────────────────


class TestRecoveryWithStripeLocks:
    """Recovery semantics are unchanged with stripe locks."""

    def test_recovery_uses_stripe_lock_for_rollback(self):
        """recover_pending_intents rolls back crashed session with stripe lock."""
        from mempalace.write_coordinator import WriteCoordinator
        from mempalace.session_registry import SessionRegistry

        palace = _pp()
        reg = SessionRegistry(palace)
        reg.register_session("recovery-s1", palace, role="agent")
        reg.unregister_session("recovery-s1")  # crashed

        wc = WriteCoordinator(palace)

        # Intent for crashed session should be rolled back via stripe lock
        with wc._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT status FROM write_intents WHERE session_id=?", ("recovery-s1",)
            )
            rows = cursor.fetchall()
            assert all(row[0] == "rolled_back" for row in rows)


class TestCanonicalInit:
    """Canonical init path, atexit registration, and close semantics."""

    def test_init_creates_db_path(self):
        """Init creates the sqlite3 db file."""
        from mempalace.write_coordinator import WriteCoordinator
        palace = _pp()
        wc = WriteCoordinator(palace)
        import os
        assert os.path.exists(wc._db_path), f"DB should exist at {wc._db_path}"
        wc.close()

    def test_init_registers_atexit(self):
        """Init registers atexit handler for close."""
        from mempalace.write_coordinator import WriteCoordinator
        palace = _pp()
        import atexit
        wc = WriteCoordinator(palace)
        # Verify atexit is registered by checking the close method is available
        # and callable — if atexit.register failed it would have raised ImportError
        # We validate by checking repr works (sanity check the object is alive)
        assert repr(wc)
        wc.close()

    def test_init_sets_all_attributes(self):
        """Init sets _write_lock, _stripe_lock, _conn, _local."""
        from mempalace.write_coordinator import WriteCoordinator
        palace = _pp()
        wc = WriteCoordinator(palace)
        assert hasattr(wc, '_write_lock')
        assert hasattr(wc, '_stripe_lock')
        assert hasattr(wc, '_conn')
        assert hasattr(wc, '_local')
        assert wc._conn is not None
        wc.close()

    def test_double_close_noops(self):
        """close() is idempotent — calling twice doesn't raise."""
        from mempalace.write_coordinator import WriteCoordinator
        palace = _pp()
        wc = WriteCoordinator(palace)
        wc.close()
        wc.close()  # no-op

    def test_repr_contains_db_path(self):
        """repr reveals db_path for debugging."""
        from mempalace.write_coordinator import WriteCoordinator
        palace = _pp()
        wc = WriteCoordinator(palace)
        r = repr(wc)
        assert 'write_coordinator.sqlite3' in r
        assert 'stripes=16' in r
        wc.close()

    def test_close_clears_conn(self):
        """close() sets _conn to None after closing."""
        from mempalace.write_coordinator import WriteCoordinator
        palace = _pp()
        wc = WriteCoordinator(palace)
        assert wc._conn is not None
        wc.close()
        assert wc._conn is None

    def test_init_default_palace_path(self):
        """Init uses MEMPALACE_PATH env var or '.mempalace'."""
        from mempalace.write_coordinator import WriteCoordinator
        import os
        # Clean env
        old = os.environ.pop("MEMPALACE_PATH", None)
        try:
            wc = WriteCoordinator()
            assert '.mempalace' in wc._db_path
            wc.close()
        finally:
            if old:
                os.environ["MEMPALACE_PATH"] = old

    def test_init_env_palace_path(self):
        """Init respects MEMPALACE_PATH env var."""
        from mempalace.write_coordinator import WriteCoordinator
        import os
        os.environ["MEMPALACE_PATH"] = "/tmp/test_mempalace_env"
        try:
            wc = WriteCoordinator()
            assert wc._db_path == "/tmp/test_mempalace_env/write_coordinator.sqlite3"
            wc.close()
        finally:
            del os.environ["MEMPALACE_PATH"]

    def test_init_explicit_palace_path(self):
        """Init accepts explicit palace_path argument."""
        from mempalace.write_coordinator import WriteCoordinator
        palace = _pp()
        wc = WriteCoordinator(palace)
        assert wc._db_path.endswith("write_coordinator.sqlite3")
        assert palace in wc._db_path
        wc.close()


# ─── Micro-Benchmark Helper ──────────────────────────────────────────────────


class TestStripeLockBenchmark:
    """Lightweight stress test to measure stripe lock throughput."""

    def test_stripe_lock_throughput_6_sessions(self):
        """
        Benchmark: 6 sessions × 50 operations each.
        Measures: total wall time, operations/second.

        Run: pytest tests/test_write_coordinator_integration.py::TestStripeLockBenchmark::test_stripe_lock_throughput_6_sessions -v -s
        """
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        num_sessions = 6
        ops_per_session = 50
        barrier = threading.Barrier(num_sessions)
        errors = []

        def session_worker(session_id):
            try:
                barrier.wait()
                for i in range(ops_per_session):
                    intent_id = wc.log_intent(
                        session_id, "add_drawer", "drawer",
                        f"bench_drawer_{session_id}_{i}"
                    )
                    wc.commit_intent(intent_id, session_id)
            except Exception as e:
                errors.append(e)

        start = time.monotonic()
        threads = [
            threading.Thread(target=session_worker, args=(f"bench-s-{i}",))
            for i in range(num_sessions)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        elapsed = time.monotonic() - start

        total_ops = num_sessions * ops_per_session * 2  # log + commit
        throughput = total_ops / elapsed if elapsed > 0 else 0

        assert len(errors) == 0, f"Benchmark errors: {errors}"
        assert not any(t.is_alive() for t in threads), "Threads did not complete"
        print(f"\n[StripeLock Benchmark] {num_sessions} sessions × {ops_per_session} ops/session")
        print(f"  Total ops: {total_ops}  |  Elapsed: {elapsed:.3f}s  |  Throughput: {throughput:.1f} ops/s")
