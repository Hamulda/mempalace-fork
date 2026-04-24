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


class TestWriteToolFTS5WarningPropagation:
    """MCP write tool handlers attach _warning to response when FTS5 sync fails.

    The LanceCollection.upsert/delete layer is tested in TestFTS5WarningReturned.
    These tests call the actual registered tool functions (not simulated patterns)
    with a mocked collection that returns an FTS5 warning, verifying the handler
    correctly attaches _warning to the response dict.
    """

    @pytest.fixture
    def write_tool_server(self):
        """Isolated server with write tools registered, collection returns FTS5 warning."""
        import tempfile
        from mempalace.server._write_tools import register_write_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = tempfile.mkdtemp(prefix="mempalace_fts5_")
        settings = MagicMock()
        settings.db_path = tmp
        settings.effective_collection_name = "test_collection"
        settings.wal_dir = tmp
        settings.palace_path = tmp
        settings.timeout_write = 30
        settings.timeout_read = 15
        settings.timeout_embed = 60

        server = MagicMock()
        server._status_cache = make_status_cache()
        server._claims_manager = None  # non-shared mode — no claim interference

        # Fake collection that returns the canonical FTS5 warning string
        self._fts5_warning = "FTS5 index sync failed — keyword search may be stale; run rebuild_keyword_index()"
        fake_col = MagicMock()
        fake_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        fake_col.upsert.return_value = self._fts5_warning
        fake_col.delete.return_value = self._fts5_warning
        fake_col.query.return_value = {
            "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[0.0]]
        }

        backend = MagicMock()
        backend.get_collection.return_value = fake_col

        config = MagicMock()
        config.palace_path = tmp

        mem_guard = MagicMock()
        mem_guard.should_pause_writes.return_value = False

        # Capture actual function objects instead of letting @server.tool() discard them
        captured = {}
        _original_tool = server.tool

        def capture_tool_decorator(**_kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator
        server.tool = capture_tool_decorator

        register_write_tools(server, backend, config, settings, mem_guard)
        server.tool = _original_tool

        return captured, fake_col

    def test_add_drawer_attaches_fts5_warning_on_failure(self, write_tool_server):
        """mempalace_add_drawer attaches _warning when col.upsert returns a warning."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_add_drawer"]
        ctx = MagicMock()

        resp = fn(
            ctx=ctx,
            wing="alpha",
            room="a1",
            content="test content",
        )

        assert resp["success"] is True
        assert "_warning" in resp, "FTS5 warning must be attached when col.upsert fails"
        assert "FTS5" in resp["_warning"]
        assert "stale" in resp["_warning"].lower()
        assert "rebuild_keyword_index" in resp["_warning"]
        fake_col.upsert.assert_called_once()

    def test_delete_drawer_attaches_fts5_warning_on_failure(self, write_tool_server):
        """mempalace_delete_drawer attaches _warning when col.delete returns a warning."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_delete_drawer"]
        ctx = MagicMock()

        # Pre-seed so delete finds something
        fake_col.get.return_value = {
            "ids": ["drawer_test_del"],
            "documents": ["existing content"],
            "metadatas": [{"wing": "w", "room": "r"}],
        }

        resp = fn(
            ctx=ctx,
            drawer_id="drawer_test_del",
        )

        assert resp["success"] is True
        assert "_warning" in resp, "FTS5 warning must be attached when col.delete fails"
        assert "FTS5" in resp["_warning"]
        assert "stale" in resp["_warning"].lower()
        assert "rebuild_keyword_index" in resp["_warning"]
        fake_col.delete.assert_called_once()

    def test_diary_write_attaches_fts5_warning_on_failure(self, write_tool_server):
        """mempalace_diary_write attaches _warning when col.upsert returns a warning."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_diary_write"]
        ctx = MagicMock()

        resp = fn(
            ctx=ctx,
            agent_name="test_agent",
            entry="test diary entry",
            topic="testing",
        )

        assert resp["success"] is True
        assert "_warning" in resp, "FTS5 warning must be attached when col.upsert fails"
        assert "FTS5" in resp["_warning"]
        assert "stale" in resp["_warning"].lower()
        assert "rebuild_keyword_index" in resp["_warning"]
        fake_col.upsert.assert_called_once()

    def test_remember_code_attaches_fts5_warning_on_failure(self, write_tool_server):
        """mempalace_remember_code attaches _warning when col.upsert returns a warning."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_remember_code"]
        ctx = MagicMock()

        resp = fn(
            ctx=ctx,
            code="print('hello')",
            description="test code memory",
            wing="beta",
            room="b2",
        )

        assert resp["success"] is True
        assert "_warning" in resp, "FTS5 warning must be attached when col.upsert fails"
        assert "FTS5" in resp["_warning"]
        assert "stale" in resp["_warning"].lower()
        assert "rebuild_keyword_index" in resp["_warning"]
        fake_col.upsert.assert_called_once()

    def test_add_drawer_no_warning_on_fts5_success(self, write_tool_server):
        """On happy path (col.upsert returns None), _warning must NOT be in response."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_add_drawer"]
        ctx = MagicMock()

        # Simulate FTS5 success: upsert returns None
        fake_col.upsert.return_value = None

        resp = fn(
            ctx=ctx,
            wing="ok",
            room="ok1",
            content="clean content",
        )

        assert resp["success"] is True
        assert "_warning" not in resp, "_warning must be absent when FTS5 sync succeeds"

    def test_delete_drawer_no_warning_on_fts5_success(self, write_tool_server):
        """On happy path (col.delete returns None), _warning must NOT be in response."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_delete_drawer"]
        ctx = MagicMock()

        fake_col.get.return_value = {
            "ids": ["drawer_ok_del"],
            "documents": ["to delete"],
            "metadatas": [{"wing": "w", "room": "r"}],
        }
        fake_col.delete.return_value = None

        resp = fn(
            ctx=ctx,
            drawer_id="drawer_ok_del",
        )

        assert resp["success"] is True
        assert "_warning" not in resp, "_warning must be absent when FTS5 sync succeeds"

    def test_diary_write_no_warning_on_fts5_success(self, write_tool_server):
        """On happy path (col.upsert returns None), _warning must NOT be in response."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_diary_write"]
        ctx = MagicMock()

        fake_col.upsert.return_value = None

        resp = fn(
            ctx=ctx,
            agent_name="clean_agent",
            entry="clean entry",
            topic="testing",
        )

        assert resp["success"] is True
        assert "_warning" not in resp, "_warning must be absent when FTS5 sync succeeds"

    def test_remember_code_no_warning_on_fts5_success(self, write_tool_server):
        """On happy path (col.upsert returns None), _warning must NOT be in response."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_remember_code"]
        ctx = MagicMock()

        fake_col.upsert.return_value = None

        resp = fn(
            ctx=ctx,
            code="def foo(): pass",
            description="clean code memory",
            wing="gamma",
            room="g1",
        )

        assert resp["success"] is True
        assert "_warning" not in resp, "_warning must be absent when FTS5 sync succeeds"

    def test_consolidate_merge_attaches_fts5_warning_on_delete_failure(self, write_tool_server):
        """mempalace_consolidate attaches _warning when merge col.delete returns a warning."""
        captured, fake_col = write_tool_server
        fn = captured["mempalace_consolidate"]
        ctx = MagicMock()

        _fts5_warning = "FTS5 index sync failed — keyword search may be stale; run rebuild_keyword_index()"

        # Mock query to return 2 duplicates (keeper + to_remove)
        fake_col.query.return_value = {
            "ids": [["dup_keeper_id", "dup_remove_id"]],
            "documents": [["keeper content", "duplicate content"]],
            "metadatas": [[
                {"wing": "w1", "room": "r1", "timestamp": "2025-01-01T00:00:00Z"},
                {"wing": "w2", "room": "r2", "timestamp": "2024-01-01T00:00:00Z"},
            ]],
            "distances": [[0.05, 0.10]],
        }
        # col.get for batch timestamp fetch
        fake_col.get.return_value = {
            "ids": [["dup_keeper_id", "dup_remove_id"]],
            "documents": [["keeper content"], ["duplicate content"]],
            "metadatas": [[
                {"wing": "w1", "room": "r1", "timestamp": "2025-01-01T00:00:00Z"},
                {"wing": "w2", "room": "r2", "timestamp": "2024-01-01T00:00:00Z"},
            ]],
        }
        # col.delete returns the FTS5 warning on the merge path
        fake_col.delete.return_value = _fts5_warning

        resp = fn(ctx=ctx, topic="dupe topic", merge=True)

        # On merge path, resp has "merged" count, not "success"
        assert resp.get("merged") == 1, f"consolidate merge failed: {resp}"
        assert "_warning" in resp, "FTS5 warning must be attached when col.delete fails in consolidate merge"
        assert "FTS5" in resp["_warning"]
        assert "stale" in resp["_warning"].lower()
        assert "rebuild_keyword_index" in resp["_warning"]
        fake_col.delete.assert_called_once()

    def test_warning_detached_if_write_tools_stops_attaching_it(self, write_tool_server):
        """Regression: fails if _write_tools.py ever removes the fts5_warning attachment.

        This test uses the real capture pattern from the actual function source.
        It would pass with the current implementation but fail if someone removes
        the `if fts5_warning: resp["_warning"] = fts5_warning` line from any handler.
        """
        captured, fake_col = write_tool_server
        fn = captured["mempalace_add_drawer"]
        ctx = MagicMock()

        # col returns a warning — if attachment is removed, this assertion fails
        fake_col.upsert.return_value = "FTS5 index sync failed — keyword search may be stale; run rebuild_keyword_index()"

        resp = fn(ctx=ctx, wing="reg", room="r1", content="regression test")

        assert "_warning" in resp, (
            "FTS5 warning must be in response — write tool handler has likely been changed "
            "to no longer attach fts5_warning to resp['_warning']"
        )
