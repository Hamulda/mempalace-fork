"""
Tests for memory-pressure-safe behavior in MemPalace Lance runtime.
Verifies that full-table materialization, large batch operations, and
embedding fallback are properly guarded.

Run: pytest tests/test_memory_pressure.py -v
"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import threading


class TestBm25BuildBounded:
    """BM25 index build must be bounded and respect memory pressure."""

    def test_bm25_loads_in_batches_not_single_full_fetch(self):
        """BM25 build loads collection in batches of 2000, not single limit=50000."""
        import mempalace.searcher as sr

        mock_col = MagicMock()
        # Simulate batched loading behavior
        mock_col.get.side_effect = [
            {"documents": ["doc1", "doc2"], "ids": ["id1", "id2"], "metadatas": [{"w": "a"}, {"w": "b"}]},
            {"documents": ["doc3"], "ids": ["id3"], "metadatas": [{"w": "c"}]},
            {"documents": [], "ids": [], "metadatas": []},
        ]

        with patch.object(sr, "_bm25_lock", threading.Lock()), \
             patch.object(sr, "_bm25_index", None), \
             patch.object(sr, "_bm25_corpus", None), \
             patch.object(sr, "_bm25_ids", None), \
             patch.object(sr, "_bm25_metas", None), \
             patch.object(sr, "_bm25_path_cached", None), \
             patch.object(sr, "logger") as mock_logger:

            with patch.dict("sys.modules", {"rank_bm25": MagicMock()}):
                import rank_bm25
                rank_bm25.BM25Okapi = MagicMock(return_value=MagicMock())

                # Reset state
                sr._bm25_index = None
                sr._bm25_path_cached = None
                sr._bm25_metas = None
                sr._bm25_ids = None
                sr._bm25_corpus = None

                result = sr._get_bm25(mock_col, "/fake/palace", max_docs=10000)

            # Verify: get was called with limit=2000 (batch size), not limit=50000
            first_call_limit = mock_col.get.call_args_list[0][1].get("limit", 0)
            assert first_call_limit == 2000, f"Expected batch size 2000, got {first_call_limit}"

    def test_bm25_respects_memory_guard_critical(self):
        """BM25 build skips when memory pressure is CRITICAL."""
        import mempalace.searcher as sr

        mock_col = MagicMock()
        mock_col.get.return_value = {"documents": [], "ids": [], "metadatas": []}

        with patch.object(sr, "_bm25_lock", threading.Lock()), \
             patch.object(sr, "logger") as mock_logger:

            sr._bm25_index = None

            with patch.dict("sys.modules", {"rank_bm25": MagicMock()}):
                import rank_bm25
                rank_bm25.BM25Okapi = MagicMock()

                with patch("mempalace.memory_guard.MemoryGuard") as mock_mg_cls, \
                     patch("mempalace.memory_guard.MemoryPressure") as mock_pressure:
                    mock_guard = MagicMock()
                    type(mock_guard).pressure = PropertyMock(return_value=mock_pressure.CRITICAL)
                    mock_mg_cls.get.return_value = mock_guard

                    result = sr._get_bm25(mock_col, "/fake/palace")

            assert result == (None, None, None, None)
            mock_logger.warning.assert_called()

    def test_bm25_max_docs_limit_halts_early(self):
        """BM25 build stops when max_docs reached."""
        import mempalace.searcher as sr

        mock_col = MagicMock()
        mock_col.get.return_value = {
            "documents": ["doc1"] * 5000,
            "ids": ["id1"] * 5000,
            "metadatas": [{"w": "a"}] * 5000,
        }

        with patch.object(sr, "_bm25_lock", threading.Lock()), \
             patch.object(sr, "logger"), \
             patch.dict("sys.modules", {"rank_bm25": MagicMock()}):
            import rank_bm25
            rank_bm25.BM25Okapi = MagicMock()

            sr._bm25_index = None
            sr._bm25_path_cached = None

            result = sr._get_bm25(mock_col, "/fake/palace", max_docs=3000)

        bm25, docs, ids, metas = result
        assert len(docs) == 3000, f"Expected 3000, got {len(docs)}"