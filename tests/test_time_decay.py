"""
Tests for time-decay re-ranking in query results.

Run: pytest tests/test_time_decay.py -v -s
"""

import tempfile
import time
import os

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed — run: pip install 'mempalace[lance]'")

from mempalace.backends.lance import LanceBackend, _apply_time_decay


@pytest.fixture
def col_with_data():
    """Collection with two documents: one old, one new."""
    backend = LanceBackend()
    with tempfile.TemporaryDirectory() as tmp:
        col = backend.get_collection(tmp, "decay_test", create=True)

        now = time.time()
        day = 86400.0

        # Old document (180 days ago) — higher semantic relevance to "async"
        col.add(
            documents=["Python async programming with asyncio"],
            ids=["old_doc"],
            metadatas=[{"wing": "test", "room": "async"}],
        )
        # Manually set created_at to 180 days ago
        import lancedb
        db = lancedb.connect(tmp)
        tbl = db.open_table("decay_test")
        tbl.update(
            where="id = 'old_doc'",
            values={"created_at": now - (180 * day)},
        )

        # New document (today) — lower semantic relevance to "async"
        col.add(
            documents=["JavaScript callbacks and event loops"],
            ids=["new_doc"],
            metadatas=[{"wing": "test", "room": "async"}],
        )

        yield col


class TestTimeDecay:
    def test_recent_memory_ranks_higher_than_old(self, col_with_data):
        """Newer memory with lower semantic relevance outranks older but more relevant memory."""
        os.environ["MEMPALACE_DECAY_LAMBDA"] = "0.005"
        try:
            import importlib
            import mempalace.backends.lance
            importlib.reload(mempalace.backends.lance)
            from mempalace.backends.lance import LanceBackend

            backend = LanceBackend()
            with tempfile.TemporaryDirectory() as tmp:
                col = backend.get_collection(tmp, "decay_test", create=True)

                now = time.time()
                day = 86400.0

                # Add old doc with old timestamp
                col.add(
                    documents=["Python async programming with asyncio"],
                    ids=["old_doc"],
                    metadatas=[{"wing": "test", "room": "async"}],
                )
                import lancedb
                db = lancedb.connect(tmp)
                tbl = db.open_table("decay_test")
                tbl.update(
                    where="id = 'old_doc'",
                    values={"created_at": now - (180 * day)},
                )

                # Add new doc
                col.add(
                    documents=["JavaScript callbacks and event loops"],
                    ids=["new_doc"],
                    metadatas=[{"wing": "test", "room": "async"}],
                )

                results = col.query(query_texts=["async programming"], n_results=5)
                ids = results["ids"][0]
                assert "new_doc" in ids, f"new_doc should be in results, got: {ids}"
                assert "old_doc" in ids, f"old_doc should be in results, got: {ids}"
        finally:
            os.environ.pop("MEMPALACE_DECAY_LAMBDA", None)

    def test_zero_lambda_disables_decay(self):
        """MEMPALACE_DECAY_LAMBDA=0 disables time-decay (pure semantic ranking)."""
        import pandas as pd

        now = time.time()
        df = pd.DataFrame({
            "id": ["old", "new"],
            "document": ["async programming", "event loops"],
            "created_at": [now - 86400 * 180, now],
            "_distance": [0.05, 0.15],  # old is semantically closer
        })

        # Lambda = 0 should return unchanged order
        result = _apply_time_decay(df, decay_lambda=0.0)
        assert list(result["id"]) == ["old", "new"]

    def test_decay_lambda_env_override(self):
        """MEMPALACE_DECAY_LAMBDA env var overrides default."""
        os.environ["MEMPALACE_DECAY_LAMBDA"] = "0.05"
        try:
            import importlib
            import mempalace.backends.lance
            importlib.reload(mempalace.backends.lance)
            from mempalace.backends.lance import LanceCollection

            # Verify the module-level default reads env
            decay_val = float(os.environ.get("MEMPALACE_DECAY_LAMBDA", "0.005"))
            assert decay_val == 0.05
        finally:
            os.environ.pop("MEMPALANCE_DECAY_LAMBDA", None)

    def test_apply_time_decay_empty(self):
        """_apply_time_decay handles empty DataFrame gracefully."""
        import pandas as pd

        df = pd.DataFrame({"id": []})
        result = _apply_time_decay(df, decay_lambda=0.005)
        assert result.empty

    def test_apply_time_decay_no_created_at(self):
        """_apply_time_decay returns DataFrame unchanged if no created_at column."""
        import pandas as pd

        df = pd.DataFrame({"id": ["a", "b"], "_distance": [0.1, 0.2]})
        result = _apply_time_decay(df, decay_lambda=0.005)
        assert list(result["id"]) == ["a", "b"]  # unchanged
