"""Batch-operation regression tests for write/delete hot path.

Verifies that:
1. delete(where=...) uses batch DELETE (WHERE id IN (...)) not per-ID deletes
2. miner tombstoning uses single batch upsert not per-ID upserts
"""
import pytest
from unittest.mock import patch, MagicMock, call


# =============================================================================
# delete(where=...) batch tests
# =============================================================================

class TestDeleteBatchBehavior:
    """Verify delete(where=...) uses batch operations via WHERE id IN (...)."""

    @pytest.fixture
    def tmp_palace_fixture(self, tmp_path):
        palace_dir = tmp_path / "palace"
        palace_dir.mkdir()
        return str(palace_dir)

    @pytest.fixture
    def lance_collection(self, tmp_palace_fixture):
        """Fresh LanceDB collection with coalescer disabled."""
        import os
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_BACKEND"] = "lance"
        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(tmp_palace_fixture, "batch_test", create=True)
        yield col

    def test_delete_by_where_uses_batch_delete(self, lance_collection):
        """delete(where=...) should issue one batch DELETE with WHERE id IN (...),
        not N individual deletes per id."""
        N = 5
        for i in range(N):
            lance_collection.add(
                documents=[f"doc {i}"],
                ids=[f"id_{i}"],
                metadatas=[{"x": i}],
            )

        delete_call_count = [0]
        delete_where_clauses = []

        _orig_delete = lance_collection._table.delete

        def track_delete(where_clause):
            delete_call_count[0] += 1
            delete_where_clauses.append(where_clause)
            return _orig_delete(where_clause)

        with patch.object(lance_collection._table, "delete", side_effect=track_delete):
            lance_collection.delete(where={"x": {"$ge": 0}})

        assert delete_call_count[0] == 1, (
            f"Expected 1 batch delete, got {delete_call_count[0]} calls: "
            f"{delete_where_clauses}"
        )
        assert "IN" in delete_where_clauses[0], (
            f"Expected WHERE id IN (...) clause, got: {delete_where_clauses[0]}"
        )

    def test_delete_by_ids_uses_single_batch(self, lance_collection):
        """delete(ids=[...]) with multiple ids should use one WHERE id IN (...) call."""
        N = 3
        lance_collection.add(
            documents=[f"doc {i}" for i in range(N)],
            ids=[f"id_{i}" for i in range(N)],
            metadatas=[{"x": i} for i in range(N)],
        )

        delete_call_count = [0]
        delete_where_clauses = []

        _orig_delete = lance_collection._table.delete

        def track_delete(where_clause):
            delete_call_count[0] += 1
            delete_where_clauses.append(where_clause)
            return _orig_delete(where_clause)

        with patch.object(lance_collection._table, "delete", side_effect=track_delete):
            lance_collection.delete(ids=[f"id_{i}" for i in range(N)])

        assert delete_call_count[0] == 1, (
            f"Expected 1 batch delete, got {delete_call_count[0]} calls: "
            f"{delete_where_clauses}"
        )
        assert "IN" in delete_where_clauses[0]


# =============================================================================
# miner tombstone batch tests
# =============================================================================

class TestMinerTombstoneBatch:
    """Verify miner tombstoning produces a single batch upsert call."""

    def test_tombstone_batches_into_single_upsert(self, tmp_path):
        """Tombstoning N old chunks should call collection.upsert once with N entries."""
        from collections import defaultdict
        from mempalace.miner import process_file
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        test_file = project_dir / "test.txt"
        test_file.write_text("new content here that is definitely longer than 50 chars for testing")

        upsert_calls = []
        mock_collection = MagicMock()
        mock_collection.upsert = lambda **kwargs: upsert_calls.append(kwargs)

        # Existing chunks with various hashes (none matching new content)
        mock_collection.get = MagicMock(return_value={
            "ids": ["old_0", "old_1", "old_2", "old_3", "old_4"],
            "metadatas": [
                {"content_hash": "hashA", "is_latest": True},
                {"content_hash": "hashA", "is_latest": True},
                {"content_hash": "hashB", "is_latest": True},
                {"content_hash": "hashC", "is_latest": True},
                {"content_hash": "hashC", "is_latest": True},
            ],
        })

        with patch("mempalace.miner._compute_content_hash") as mock_hash:
            # New content gets a hash that does NOT match any old hash
            mock_hash.side_effect = lambda c: (
                "new_hash_for_new_content" if c.startswith("new") else c
            )
            count, room = process_file(
                filepath=test_file,
                project_path=Path(tmp_path),
                collection=mock_collection,
                wing="test_wing",
                rooms=[{"name": "general", "keywords": []}],
                agent="test_agent",
                dry_run=False,
            )

        # Total upsert calls: 1 for new chunks + 1 for tombstone batch
        assert len(upsert_calls) == 2, f"Expected 2 upsert calls, got {len(upsert_calls)}: {[c['ids'] for c in upsert_calls]}"

        # Second call is the tombstone batch (all 5 old chunks)
        tombstone_call = upsert_calls[1]
        assert len(tombstone_call["ids"]) == 5, (
            f"Expected 5 tombstone ids in batch, got {len(tombstone_call['ids'])}: "
            f"{tombstone_call['ids']}"
        )
        assert all(
            m.get("is_latest") is False for m in tombstone_call["metadatas"]
        ), "All tombstone entries must have is_latest=False"

    def test_tombstone_skipped_when_all_superseded(self, tmp_path):
        """When all old chunks are superseded by new chunks, no tombstone upsert needed."""
        from collections import defaultdict
        from mempalace.miner import process_file
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        test_file = project_dir / "test.txt"
        test_file.write_text("old content that stays the same and is definitely long enough")

        upsert_calls = []
        mock_collection = MagicMock()
        mock_collection.upsert = lambda **kwargs: upsert_calls.append(kwargs)

        # Return one existing chunk with hash matching the file content
        mock_collection.get = MagicMock(return_value={
            "ids": ["existing_id"],
            "metadatas": [
                {"content_hash": "same_hash", "is_latest": True},
            ],
        })

        with patch("mempalace.miner._compute_content_hash") as mock_hash:
            # New content has SAME hash as existing → supersedes it, no tombstone needed
            mock_hash.return_value = "same_hash"
            count, room = process_file(
                filepath=test_file,
                project_path=Path(tmp_path),
                collection=mock_collection,
                wing="test_wing",
                rooms=[{"name": "general", "keywords": []}],
                agent="test_agent",
                dry_run=False,
            )

        # Only 1 upsert for the re-upserted content (same hash → superseded, no tombstone batch)
        assert len(upsert_calls) == 1, (
            f"Expected 1 upsert (re-upsert existing, no tombstone), got {len(upsert_calls)}: "
            f"{[c['ids'] for c in upsert_calls]}"
        )
