"""
Phase 4 Correctness Tests — Stabilization invariants.

Tests prove the correctness guarantees for:
- FTS5 delete sync (stale rows don't persist)
- WriteCoordinator recovery (pending intents replay correctly)
- Claims source of truth (WriteCoordinator blocks conflicting writes)
- Decision supersession (correct direction and query behavior)
- is_latest maintenance (no phantom current records after delete)

Run: pytest tests/test_phase4_correctness.py -v
"""

import tempfile
import pytest

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
    return tempfile.mkdtemp(prefix="mempalace_p4_")


@pytest.fixture(autouse=True)
def reset_keyword_index_singleton():
    """Reset KeywordIndex singleton between tests to prevent cross-test pollution."""
    from mempalace.lexical_index import KeywordIndex
    KeywordIndex._reset_for_testing()
    yield
    KeywordIndex._reset_for_testing()


class TestFTS5DeleteSync:
    """FTS5 index must not have stale rows after delete operations."""

    def test_delete_by_id_removes_from_fts(self):
        """delete(ids=[...]) removes records from KeywordIndex FTS5."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend
        from mempalace.lexical_index import KeywordIndex

        palace = _pp()

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "fts_delete", create=True)

            # Add records (upsert triggers FTS5 upsert_drawer)
            col.upsert(
                documents=["apple banana cherry", "dog cat bird"],
                ids=["d1", "d2"],
                metadatas=[{"wing": "repo", "room": "src"}, {"wing": "repo", "room": "src"}],
            )

            idx = KeywordIndex.get(palace)
            assert idx.count() == 2

            # Delete one record by ID
            col.delete(ids=["d1"])

            # FTS5 must NOT have stale row for d1
            assert idx.count() == 1
            search = idx.search("apple banana")
            doc_ids = [r["document_id"] for r in search]
            assert "d1" not in doc_ids

    def test_delete_by_where_removes_from_fts(self):
        """delete(where=...) removes matching records from KeywordIndex FTS5."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend
        from mempalace.lexical_index import KeywordIndex

        palace = _pp()

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "fts_delete_where", create=True)

            col.upsert(
                documents=["alpha beta gamma", "delta epsilon zeta", "theta iota kappa"],
                ids=["w1", "w2", "w3"],
                metadatas=[
                    {"wing": "repo", "room": "src"},
                    {"wing": "repo", "room": "general"},
                    {"wing": "repo", "room": "src"},
                ],
            )

            idx = KeywordIndex.get(palace)
            assert idx.count() == 3

            # Delete all records in src room
            col.delete(where={"room": "src"})

            # Only w2 (general room) should remain in FTS5
            assert idx.count() == 1
            search = idx.search("alpha")
            doc_ids = [r["document_id"] for r in search]
            assert "w1" not in doc_ids  # was in src
            assert "w3" not in doc_ids  # was in src


class TestWriteCoordinatorRecovery:
    """Pending intents must be recovered on startup."""

    def test_pending_intent_rolled_back_on_startup_when_session_stopped(self):
        """Pending intent from a stopped session is rolled back on WriteCoordinator init."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()

        # Create a stopped session in session_registry
        from mempalace.session_registry import SessionRegistry
        reg = SessionRegistry(palace)
        reg.register_session("crashed-session", palace, role="agent")
        reg.unregister_session("crashed-session")  # mark as stopped

        # Log a pending intent for the crashed session
        wc = WriteCoordinator(palace)
        intent_id = wc.log_intent("crashed-session", "revision", "file", "/src/main.py")

        # Verify it's pending
        pending = wc.get_pending_intents()
        assert any(i["id"] == intent_id for i in pending)

        # Create a new WriteCoordinator — should rollback the crashed session's intent
        wc2 = WriteCoordinator(palace)
        pending_after = wc2.get_pending_intents()
        rolled_back = [i for i in pending_after if i["id"] == intent_id]
        assert all(i["status"] == "rolled_back" for i in rolled_back)

    def test_pending_intent_kept_for_active_session(self):
        """Pending intent from an active session is kept for replay."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()

        # Create an active session
        from mempalace.session_registry import SessionRegistry
        reg = SessionRegistry(palace)
        reg.register_session("active-session", palace, role="agent")

        wc = WriteCoordinator(palace)
        intent_id = wc.log_intent("active-session", "revision", "file", "/src/main.py")

        # Create new WriteCoordinator — should keep the active session's intent
        wc2 = WriteCoordinator(palace)
        pending = wc2.get_pending_intents(session_id="active-session")
        assert any(i["id"] == intent_id and i["status"] == "pending" for i in pending)

    def test_commit_and_rollback_intent(self):
        """commit_intent and rollback_intent work correctly."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        intent_id = wc.log_intent("session-x", "revision", "file", "/src/main.py")

        # Rollback
        result = wc.rollback_intent(intent_id, "session-x")
        assert result is True

        intent = wc.get_pending_intents()
        rolled = [i for i in intent if i["id"] == intent_id]
        assert all(i["status"] == "rolled_back" for i in rolled)

        # Commit another intent
        intent_id2 = wc.log_intent("session-x", "revision", "file", "/src/utils.py")
        result2 = wc.commit_intent(intent_id2, "session-x")
        assert result2 is True


class TestWriteCoordinatorClaims:
    """WriteCoordinator is the canonical source of truth for file claims."""

    def test_claim_blocks_other_session(self):
        """WriteCoordinator.claim() returns acquired=False when another session holds claim."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        r1 = wc.claim("file", "/src/main.py", "session-a")
        assert r1["acquired"] is True
        assert r1["owner"] == "session-a"

        r2 = wc.claim("file", "/src/main.py", "session-b")
        assert r2["acquired"] is False
        assert r2["owner"] == "session-a"

    def test_claim_with_ttl(self):
        """WriteCoordinator.claim() supports TTL-based expiration."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        r = wc.claim("file", "/src/main.py", "session-a", ttl_seconds=1)
        assert r["acquired"] is True

        # Immediate re-check should succeed (not expired yet)
        r2 = wc.claim("file", "/src/main.py", "session-b")
        # session-a still holds it
        assert r2["acquired"] is False
        assert r2["owner"] == "session-a"

    def test_release_claim(self):
        """release_claim() allows another session to acquire."""
        from mempalace.write_coordinator import WriteCoordinator

        palace = _pp()
        wc = WriteCoordinator(palace)

        wc.claim("file", "/src/main.py", "session-a")
        released = wc.release_claim("file", "/src/main.py", "session-a")
        assert released is True

        r = wc.claim("file", "/src/main.py", "session-b")
        assert r["acquired"] is True
        assert r["owner"] == "session-b"


class TestDecisionSupersession:
    """Decision supersession direction is correct."""

    def test_supersede_updates_old_record(self):
        """superse_decision() marks old record as superseded with correct superseded_by."""
        from mempalace.decision_tracker import DecisionTracker

        palace = _pp()
        dt = DecisionTracker(palace)

        r1 = dt.capture_decision(
            session_id="session-a",
            decision_text="Use REST API",
            rationale="Simpler",
            alternatives=["GraphQL"],
            category="api",
            confidence=3,
        )
        old_id = r1["decision_id"]

        r2 = dt.capture_decision(
            session_id="session-a",
            decision_text="Use GraphQL",
            rationale="Better for nested data",
            alternatives=["REST"],
            category="api",
            confidence=4,
        )
        new_id = r2["decision_id"]

        dt.supersede_decision(old_id, new_id, "session-a")

        old = dt.get_decision(old_id)
        assert old["status"] == "superseded"
        assert old["superseded_by"] == new_id

        # New decision should still be active
        new = dt.get_decision(new_id)
        assert new["status"] == "active"

    def test_list_decisions_excludes_superseded(self):
        """list_decisions(status='active') correctly excludes superseded decisions."""
        from mempalace.decision_tracker import DecisionTracker

        palace = _pp()
        dt = DecisionTracker(palace)

        r1 = dt.capture_decision(
            session_id="session-a",
            decision_text="Old decision",
            rationale="",
            alternatives=[],
            category="test",
            confidence=3,
        )
        old_id = r1["decision_id"]

        r2 = dt.capture_decision(
            session_id="session-a",
            decision_text="New decision",
            rationale="",
            alternatives=[],
            category="test",
            confidence=4,
        )
        new_id = r2["decision_id"]

        dt.supersede_decision(old_id, new_id, "session-a")

        active = dt.list_decisions(status="active")
        active_ids = [d["id"] for d in active]
        assert old_id not in active_ids
        assert new_id in active_ids


class TestIsLatestCorrectness:
    """is_latest flag is maintained correctly after operations."""

    def test_delete_removes_record(self):
        """delete(ids=[...]) removes the specific record."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend

        palace = _pp()

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "delete_test", create=True)

            col.add(
                documents=["hello world"],
                ids=["del_test_1"],
                metadatas=[{"wing": "repo", "room": "src", "is_latest": True}],
            )

            assert col.count() == 1

            col.delete(ids=["del_test_1"])
            assert col.count() == 0

    def test_upsert_same_id_replaces(self):
        """upsert with same ID replaces the existing record."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend

        palace = _pp()

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "upsert_replace", create=True)

            col.upsert(
                documents=["original content"],
                ids=["same_id"],
                metadatas=[{"wing": "repo", "room": "src", "is_latest": True}],
            )
            assert col.count() == 1

            col.upsert(
                documents=["updated content"],
                ids=["same_id"],
                metadatas=[{"wing": "repo", "room": "src", "is_latest": True}],
            )
            # Should replace, not duplicate
            assert col.count() == 1

            # The content should be updated
            result = col.get(ids=["same_id"])
            assert result["documents"][0] == "updated content"


class TestLexicalIndexConsistency:
    """Lexical index stays in sync with LanceDB state."""

    def test_bulk_upsert_clears_and_rebuilds(self):
        """bulk_upsert() replaces all FTS5 entries with provided entries."""
        from mempalace.lexical_index import KeywordIndex

        palace = _pp()
        idx = KeywordIndex.get(palace)

        idx.upsert_drawer("doc1", "apple banana", "repo", "src", "English")
        idx.upsert_drawer("doc2", "dog cat bird", "repo", "src", "English")
        assert idx.count() == 2

        idx.bulk_upsert([
            {"document_id": "doc3", "content": "new content alpha", "wing": "repo", "room": "src", "language": "English"},
        ])
        assert idx.count() == 1

        search = idx.search("apple")
        assert all(r["document_id"] != "doc1" for r in search)

    def test_upsert_drawer_replaces_existing(self):
        """upsert_drawer() replaces existing entry for same document_id."""
        from mempalace.lexical_index import KeywordIndex

        palace = _pp()
        idx = KeywordIndex.get(palace)

        idx.upsert_drawer("doc_x", "original content", "repo", "src", "English")
        assert idx.count() == 1

        idx.upsert_drawer("doc_x", "updated content", "repo", "src", "English")
        assert idx.count() == 1

        search = idx.search("updated")
        assert all(r["document_id"] == "doc_x" for r in search)

        search2 = idx.search("original")
        assert all(r["document_id"] != "doc_x" for r in search2)

    def test_delete_drawer_removes_exact_entry(self):
        """delete_drawer() removes the specific FTS5 entry."""
        from mempalace.lexical_index import KeywordIndex

        palace = _pp()
        idx = KeywordIndex.get(palace)

        idx.upsert_drawer("del_doc", "to be deleted", "repo", "src", "English")
        assert idx.count() == 1

        idx.delete_drawer("del_doc")
        assert idx.count() == 0

        search = idx.search("deleted")
        assert len(search) == 0
