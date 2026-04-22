"""
test_lexical_freshness.py — Tests for FTS5 incremental sync.

Verifies that writes (add/upsert/delete) to LanceCollection immediately
populate the FTS5 keyword index, closing the lexical freshness gap.

Scope:
- LanceCollection._sync_fts5upsert: write path → FTS5 upsert
- LanceCollection._sync_fts5delete: delete path → FTS5 delete
- KeywordIndex.upsert_drawer / delete_drawer integration
- Diary/workflow-generated writes via upsert
- No regression in LanceDB write correctness
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from mempalace.lexical_index import KeywordIndex

pytest.importorskip("lancedb", reason="LanceDB required")


# ── Deterministic mock embeddings ─────────────────────────────────────────


def _mock_embed_texts(texts):
    """Deterministic fake embeddings — bypasses MLX daemon and memory guard."""
    import hashlib
    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_palace(tmp_path):
    """Temporary palace directory."""
    palace = tmp_path / "palace"
    palace.mkdir()
    KeywordIndex._reset_for_testing()
    return str(palace)


@pytest.fixture
def lance_collection(temp_palace):
    """LanceDB collection with mocked embeddings, no coalescer."""
    import os
    os.environ["MEMPALACE_COALESCE_MS"] = "0"
    os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
    os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

    import mempalace.backends.lance as lance_module
    original = lance_module._embed_texts
    lance_module._embed_texts = _mock_embed_texts

    from mempalace.backends.lance import LanceBackend
    backend = LanceBackend()
    col = backend.get_collection(temp_palace, "test_lexical", create=True)

    yield col

    lance_module._embed_texts = original


# ── Test: KeywordIndex.upsert_drawer / delete_drawer directly ─────────────


class TestKeywordIndexDirect:
    """Unit tests for KeywordIndex write methods."""

    def test_upsert_drawer_inserts_and_retrieves(self, temp_palace):
        """upsert_drawer → document findable via search()."""
        idx = KeywordIndex.get(temp_palace)
        idx.upsert_drawer(
            document_id="doc1",
            content="JWT RS256 authentication",
            wing="auth",
            room="security",
            language="python",
        )
        results = idx.search("JWT", n_results=5)
        assert any(r["document_id"] == "doc1" for r in results)

    def test_upsert_drawer_replaces_existing(self, temp_palace):
        """Second upsert_drawer with same document_id replaces content."""
        idx = KeywordIndex.get(temp_palace)
        idx.upsert_drawer("doc_replace", "original text", "w", "r")
        idx.upsert_drawer("doc_replace", "replaced text", "w", "r")

        orig = idx.search("original", n_results=5)
        repl = idx.search("replaced", n_results=5)
        assert not any(r["document_id"] == "doc_replace" for r in orig)
        assert any(r["document_id"] == "doc_replace" for r in repl)

    def test_delete_drawer_removes_from_index(self, temp_palace):
        """delete_drawer → document no longer findable."""
        idx = KeywordIndex.get(temp_palace)
        idx.upsert_drawer("doc_del", "to be deleted content", "w", "r")
        assert idx.search("deleted", n_results=5)

        idx.delete_drawer("doc_del")

        results = idx.search("deleted", n_results=5)
        assert not any(r["document_id"] == "doc_del" for r in results)

    def test_delete_drawer_nonexistent_is_noop(self, temp_palace):
        """Deleting non-existent document_id does not raise."""
        idx = KeywordIndex.get(temp_palace)
        before = idx.count()
        idx.delete_drawer("nonexistent_xyz")
        assert idx.count() == before

    def test_count_after_bulk_upsert(self, temp_palace):
        """Multiple upsert_drawer calls → count reflects all docs."""
        idx = KeywordIndex.get(temp_palace)
        for i in range(10):
            idx.upsert_drawer(f"doc_{i}", f"content {i}", "wing1", "room1")
        assert idx.count() >= 10


# ── Test: LanceCollection._sync_fts5 upsert integration ───────────────────


class TestLanceCollectionFTS5Sync:
    """LanceCollection writes → FTS5 immediately populated."""

    def test_upsert_syncs_to_fts5(self, lance_collection, temp_palace):
        """LanceCollection.upsert → FTS5 immediately populated."""
        doc_id = "sync_doc_1"
        content = "def authenticate_user login endpoint"
        wing = "auth"
        room = "security"
        language = "python"

        lance_collection.upsert(
            documents=[content],
            ids=[doc_id],
            metadatas=[{
                "wing": wing, "room": room,
                "language": language, "source_file": "auth.py",
            }],
        )

        idx = KeywordIndex.get(temp_palace)
        results = idx.search("authenticate", n_results=5)
        doc_ids_found = [r["document_id"] for r in results]
        assert doc_id in doc_ids_found, (
            f"FTS5 search for 'authenticate' returned {doc_ids_found}; "
            f"expected {doc_id} immediately after upsert"
        )

    def test_upsert_replaces_in_fts5(self, lance_collection, temp_palace):
        """Second upsert of same id replaces FTS5 content."""
        doc_id = "sync_doc_replace"
        lance_collection.upsert(
            documents=["original content"],
            ids=[doc_id],
            metadatas=[{"wing": "w", "room": "r", "language": ""}],
        )
        lance_collection.upsert(
            documents=["updated content"],
            ids=[doc_id],
            metadatas=[{"wing": "w", "room": "r", "language": ""}],
        )

        idx = KeywordIndex.get(temp_palace)
        assert not any(r["document_id"] == doc_id for r in idx.search("original", n_results=5))
        assert any(r["document_id"] == doc_id for r in idx.search("updated", n_results=5))

    def test_delete_syncs_to_fts5(self, lance_collection, temp_palace):
        """LanceCollection.delete(ids=[...]) → FTS5 entry removed."""
        doc_id = "sync_doc_del"

        lance_collection.upsert(
            documents=["temporary content"],
            ids=[doc_id],
            metadatas=[{"wing": "w", "room": "r", "language": ""}],
        )

        idx = KeywordIndex.get(temp_palace)
        assert idx.search("temporary", n_results=5)

        lance_collection.delete(ids=[doc_id])

        results = idx.search("temporary", n_results=5)
        assert not any(r["document_id"] == doc_id for r in results), (
            "Deleted doc should not appear in FTS5"
        )

    def test_delete_empty_ids_is_noop(self, lance_collection, temp_palace):
        """delete(ids=[]) → no crash, FTS5 unchanged."""
        idx = KeywordIndex.get(temp_palace)
        before = idx.count()
        lance_collection.delete(ids=[])
        assert idx.count() == before

    def test_delete_nonexistent_is_noop(self, lance_collection, temp_palace):
        """delete(ids=['nonexistent']) → no crash, FTS5 unchanged."""
        idx = KeywordIndex.get(temp_palace)
        before = idx.count()
        lance_collection.delete(ids=["nonexistent_xyz_123"])
        assert idx.count() == before


# ── Test: diary / workflow writes populate FTS5 ───────────────────────────


class TestDiaryWorkflowFTS5:
    """Diary and workflow patterns (upsert-based) → FTS5 visible."""

    def test_diary_entry_findable_immediately(self, lance_collection, temp_palace):
        """Diary write pattern → FTS5 findable without rebuild."""
        entry_id = "diary_test_001"
        entry_text = "Claude session started — decision: use JWT RS256 for auth"

        lance_collection.upsert(
            documents=[entry_text],
            ids=[entry_id],
            metadatas=[{
                "wing": "wing_claude",
                "room": "diary",
                "source_file": "diary://claude/2026-04-22",
                "origin_type": "diary_entry",
                "is_latest": True,
                "language": "",
            }],
        )

        idx = KeywordIndex.get(temp_palace)
        results = idx.search("JWT", n_results=5)
        assert any(r["document_id"] == entry_id for r in results), (
            "Diary entry should be lexically findable immediately after write"
        )

    def test_handoff_content_findable_immediately(self, lance_collection, temp_palace):
        """Handoff pattern → FTS5 findable without rebuild."""
        handoff_id = "handoff_session_abc"
        handoff_text = "Handing off to session def — key decision: deferred rendering"

        lance_collection.upsert(
            documents=[handoff_text],
            ids=[handoff_id],
            metadatas=[{
                "wing": "wing_session_abc",
                "room": "handoff",
                "origin_type": "handoff",
                "is_latest": True,
                "language": "",
            }],
        )

        idx = KeywordIndex.get(temp_palace)
        results = idx.search("deferred", n_results=5)
        assert any(r["document_id"] == handoff_id for r in results)

    def test_remember_code_findable_immediately(self, lance_collection, temp_palace):
        """remember_code pattern → FTS5 findable without rebuild."""
        code_id = "remember_code_test"
        code_text = "def parse_once_already_applied(): pass  # NO-OP optimization"

        lance_collection.upsert(
            documents=[code_text],
            ids=[code_id],
            metadatas=[{
                "wing": "wing_claude",
                "room": "memory",
                "origin_type": "remember_code",
                "language": "python",
            }],
        )

        idx = KeywordIndex.get(temp_palace)
        results = idx.search("parse_once", n_results=5)
        assert any(r["document_id"] == code_id for r in results)


# ── Test: FTS5 failures are gracefully contained ───────────────────────────


class TestFTS5FailureContainment:
    """FTS5 sync failures never crash the write path."""

    def test_fts5_upsert_failure_does_not_crash_write_path(self, temp_palace):
        """KeywordIndex.upsert_drawer failure → LanceDB write still succeeds."""
        import os
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_fail1", create=True)

        try:
            with patch("mempalace.lexical_index.KeywordIndex") as mock_ki_cls:
                mock_ki = MagicMock()
                mock_ki.upsert_drawer.side_effect = RuntimeError("FTS5 I/O error")
                mock_ki_cls.get.return_value = mock_ki

                # Must not raise — FTS5 failures are contained
                col.upsert(
                    documents=["content"],
                    ids=["test_doc_fail"],
                    metadatas=[{"wing": "w", "room": "r", "language": ""}],
                )

            # LanceDB write succeeded — doc exists
            result = col.get(ids=["test_doc_fail"])
            assert "test_doc_fail" in (result.get("ids") or [])
        finally:
            lance_module._embed_texts = original

    def test_fts5_delete_failure_does_not_crash_delete_path(self, temp_palace):
        """KeywordIndex.delete_drawer failure → LanceDB delete still succeeds."""
        import os
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_fail2", create=True)

        try:
            col.upsert(
                documents=["to be deleted"],
                ids=["test_doc_del_fail"],
                metadatas=[{"wing": "w", "room": "r", "language": ""}],
            )

            with patch("mempalace.lexical_index.KeywordIndex") as mock_ki_cls:
                mock_ki = MagicMock()
                mock_ki.delete_drawer.side_effect = RuntimeError("FTS5 write error")
                mock_ki_cls.get.return_value = mock_ki

                # Must not raise — FTS5 failures are contained
                col.delete(ids=["test_doc_del_fail"])

            result = col.get(ids=["test_doc_del_fail"])
            assert "test_doc_del_fail" not in (result.get("ids") or [])
        finally:
            lance_module._embed_texts = original


# ── Test: no regression in write behavior ─────────────────────────────────


class TestWriteBehaviorNoRegression:
    """Existing write correctness is preserved."""

    def test_upsert_completes_without_raising(self, lance_collection):
        """upsert() with mocked embeddings completes without raising."""
        lance_collection.upsert(
            documents=["test content for regression"],
            ids=["regression_upsert_doc"],
            metadatas=[{"wing": "w", "room": "r", "language": ""}],
        )

    def test_delete_completes_without_raising(self, lance_collection):
        """delete(ids=[...]) completes without raising."""
        doc_id = "regression_del_doc"
        lance_collection.upsert(
            documents=["to delete"],
            ids=[doc_id],
            metadatas=[{"wing": "w", "room": "r", "language": ""}],
        )
        lance_collection.delete(ids=[doc_id])

    def test_query_cache_invalidation_still_called(self, temp_palace):
        """query cache is still invalidated after writes (not regressed)."""
        import os
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_regress3", create=True)

        try:
            with patch("mempalace.query_cache.get_query_cache") as mock_cache:
                mock_cache.return_value = MagicMock()
                col.upsert(
                    documents=["cache test"],
                    ids=["cache_test_doc"],
                    metadatas=[{"wing": "w", "room": "r", "language": ""}],
                )
                mock_cache.return_value.invalidate_collection.assert_called()
        finally:
            lance_module._embed_texts = original
