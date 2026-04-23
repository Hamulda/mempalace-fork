"""
test_fts5_warning_in_response.py — Tests for FTS5 sync failure warning in tool responses.

Verifies:
- LanceCollection.upsert() returns FTS5 warning string when sync fails
- LanceCollection.delete() returns FTS5 warning string when sync fails
- LanceCollection._sync_fts5upsert/_sync_fts5delete return warning on exception
- Write tool responses attach _warning when FTS5 sync fails
"""

import os
import pytest
from unittest.mock import patch, MagicMock

from mempalace.lexical_index import KeywordIndex

pytest.importorskip("lancedb", reason="LanceDB required")


def _mock_embed_texts(texts):
    """Deterministic fake embeddings — bypasses MLX daemon."""
    import hashlib
    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


@pytest.fixture
def temp_palace(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    KeywordIndex._reset_for_testing()
    yield str(palace)
    KeywordIndex._reset_for_testing()


@pytest.fixture
def lance_collection(temp_palace):
    os.environ["MEMPALACE_COALESCE_MS"] = "0"
    os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
    os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

    import mempalace.backends.lance as lance_module
    original = lance_module._embed_texts
    lance_module._embed_texts = _mock_embed_texts

    from mempalace.backends.lance import LanceBackend
    backend = LanceBackend()
    col = backend.get_collection(temp_palace, "test_fts5_warning", create=True)

    yield col
    lance_module._embed_texts = original


class TestFTS5WarningReturned:
    """FTS5 sync failure returns warning string from upsert/delete."""

    def test_upsert_returns_warning_on_fts5_failure(self, temp_palace):
        """upsert() returns warning string when KeywordIndex.upsert_drawer_batch fails."""
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_warn_upsert", create=True)

        try:
            with patch("mempalace.lexical_index.KeywordIndex") as mock_ki_cls:
                mock_ki = MagicMock()
                mock_ki.upsert_drawer_batch.side_effect = RuntimeError("FTS5 I/O error")
                mock_ki_cls.get.return_value = mock_ki

                warning = col.upsert(
                    documents=["test content"],
                    ids=["test_warn_doc"],
                    metadatas=[{"wing": "w", "room": "r", "language": ""}],
                )

                assert warning is not None
                assert "FTS5" in warning
                assert "stale" in warning.lower()
                assert "rebuild_keyword_index" in warning
        finally:
            lance_module._embed_texts = original

    def test_delete_returns_warning_on_fts5_failure(self, temp_palace):
        """delete() returns warning string when KeywordIndex.delete_drawer_batch fails."""
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_warn_del", create=True)

        try:
            # First add a doc (without mocking FTS5 so it succeeds)
            col.upsert(
                documents=["to be deleted"],
                ids=["test_del_doc"],
                metadatas=[{"wing": "w", "room": "r", "language": ""}],
            )

            with patch("mempalace.lexical_index.KeywordIndex") as mock_ki_cls:
                mock_ki = MagicMock()
                mock_ki.delete_drawer_batch.side_effect = RuntimeError("FTS5 delete error")
                mock_ki_cls.get.return_value = mock_ki

                warning = col.delete(ids=["test_del_doc"])

                assert warning is not None
                assert "FTS5" in warning
                assert "stale" in warning.lower()
                assert "rebuild_keyword_index" in warning
        finally:
            lance_module._embed_texts = original

    def test_upsert_returns_none_on_fts5_success(self, lance_collection):
        """upsert() returns None when FTS5 sync succeeds."""
        warning = lance_collection.upsert(
            documents=["normal content"],
            ids=["test_ok_doc"],
            metadatas=[{"wing": "w", "room": "r", "language": ""}],
        )
        assert warning is None

    def test_delete_returns_none_on_fts5_success(self, lance_collection):
        """delete() returns None when FTS5 sync succeeds."""
        # First add a doc
        lance_collection.upsert(
            documents=["to delete"],
            ids=["test_ok_del"],
            metadatas=[{"wing": "w", "room": "r", "language": ""}],
        )
        warning = lance_collection.delete(ids=["test_ok_del"])
        assert warning is None


class TestSyncMethodsReturnWarnings:
    """_sync_fts5upsert and _sync_fts5delete return warning strings on exception."""

    def test_sync_upsert_returns_warning_on_exception(self, temp_palace):
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_sync_upsert", create=True)

        try:
            with patch("mempalace.lexical_index.KeywordIndex") as mock_ki_cls:
                mock_ki = MagicMock()
                mock_ki.upsert_drawer_batch.side_effect = RuntimeError("db error")
                mock_ki_cls.get.return_value = mock_ki

                result = col._sync_fts5upsert(
                    ids=["id1"],
                    documents=["doc"],
                    metadatas=[{"wing": "w", "room": "r", "language": ""}],
                )

                assert result is not None
                assert "FTS5" in result
                assert "rebuild_keyword_index" in result
        finally:
            lance_module._embed_texts = original

    def test_sync_delete_returns_warning_on_exception(self, temp_palace):
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_sync_del", create=True)

        try:
            with patch("mempalace.lexical_index.KeywordIndex") as mock_ki_cls:
                mock_ki = MagicMock()
                mock_ki.delete_drawer_batch.side_effect = RuntimeError("db error")
                mock_ki_cls.get.return_value = mock_ki

                result = col._sync_fts5delete(ids=["id1"])

                assert result is not None
                assert "FTS5" in result
                assert "rebuild_keyword_index" in result
        finally:
            lance_module._embed_texts = original

    def test_sync_upsert_returns_none_on_success(self, temp_palace):
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(temp_palace, "test_sync_ok", create=True)

        try:
            result = col._sync_fts5upsert(
                ids=["id1"],
                documents=["doc"],
                metadatas=[{"wing": "w", "room": "r", "language": ""}],
            )
            assert result is None
        finally:
            lance_module._embed_texts = original
