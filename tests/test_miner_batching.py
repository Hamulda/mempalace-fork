"""
Test: miner.py batch filing.

Verifies:
- Same chunk count as before batching
- Same room assignment
- Fewer backend write calls (N chunks → 1 upsert per file)
- source_mtime preserved
- dry-run unchanged
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from collections import defaultdict

from mempalace.miner import (
    chunk_text,
    detect_room,
    process_file,
    mine,
    CHUNK_SIZE,
)


class TestChunkCountUnchanged:
    """Ensure batching doesn't lose or duplicate chunks."""

    def test_chunk_count_same_as_before(self, tmp_path):
        """Chunk count for a file must be identical regardless of batching."""
        content = "\n\n".join([f"paragraph {i} " + "x" * 200 for i in range(50)])
        f = tmp_path / "test.py"
        f.write_text(content)

        # Track upsert call args instead of add_drawer (no longer called)
        upsert_calls = []
        def mock_upsert(documents, ids, metadatas):
            upsert_calls.append((list(documents), list(ids), list(metadatas)))

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_col.upsert = mock_upsert

        rooms = [{"name": "general", "keywords": []}]

        drawers, room = process_file(
            filepath=f,
            project_path=tmp_path,
            collection=mock_col,
            wing="testwing",
            rooms=rooms,
            agent="test",
            dry_run=False,
        )

        expected_chunks = len(chunk_text(content, str(f)))

        assert drawers == expected_chunks, (
            f"Expected {expected_chunks} drawers, got {drawers}"
        )
        # Batching: 1 upsert call with all chunks
        assert len(upsert_calls) == 1
        assert len(upsert_calls[0][0]) == expected_chunks, (
            f"Upsert carried {len(upsert_calls[0][0])} chunks, expected {expected_chunks}"
        )
        assert room == "general"


class TestBackendWriteReduction:
    """Verify batch filing reduces upsert calls."""

    def test_one_upsert_per_file_not_per_chunk(self, tmp_path, monkeypatch):
        """With batching, one file with N chunks should produce exactly 1 upsert call."""
        content = "\n\n".join([f"para {i} " + "y" * 300 for i in range(20)])
        f = tmp_path / "module.py"
        f.write_text(content)

        upsert_calls = []

        def mock_upsert(documents, ids, metadatas):
            upsert_calls.append((list(documents), list(ids), list(metadatas)))

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_col.upsert = mock_upsert

        rooms = [{"name": "general", "keywords": []}]

        drawers, _ = process_file(
            filepath=f,
            project_path=tmp_path,
            collection=mock_col,
            wing="wing",
            rooms=rooms,
            agent="test",
            dry_run=False,
        )

        expected_chunks = len(chunk_text(content, str(f)))
        assert drawers == expected_chunks
        # Batching: 1 upsert call (with all chunks), not N calls (1 per chunk)
        assert len(upsert_calls) == 1, (
            f"Expected 1 upsert call, got {len(upsert_calls)}. "
            f"Chunks: {expected_chunks}"
        )
        # The single upsert carries all chunks
        assert len(upsert_calls[0][0]) == expected_chunks


class TestRoomAssignmentUnchanged:
    """Room routing must not change with batching."""

    def test_detect_room_same_result(self, tmp_path):
        content = "def test_foo():\n    pass\n" * 50
        f = tmp_path / "test_foo.py"
        f.write_text(content)

        rooms = [
            {"name": "tests", "keywords": ["test", "spec"]},
            {"name": "general", "keywords": []},
        ]

        detected = detect_room(f, content, rooms, tmp_path)
        assert detected == "tests"


class TestSourceMtimePreserved:
    """source_mtime must be included in batch metadata."""

    def test_mtime_in_batch_metadata(self, tmp_path, monkeypatch):
        content = "z" * 1000
        f = tmp_path / "file.py"
        f.write_text(content)
        expected_mtime = f.stat().st_mtime

        upsert_calls = []
        def mock_upsert(documents, ids, metadatas):
            upsert_calls.append((list(documents), list(ids), list(metadatas)))

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_col.upsert = mock_upsert

        rooms = [{"name": "general", "keywords": []}]
        process_file(f, tmp_path, mock_col, "wing", rooms, "test", False)

        assert len(upsert_calls) == 1
        metas = upsert_calls[0][2]
        assert len(metas) == len(chunk_text(content, str(f)))
        for m in metas:
            assert "source_mtime" in m, "source_mtime missing from batch metadata"
            assert abs(m["source_mtime"] - expected_mtime) < 1


class TestDryRunUnchanged:
    """Dry-run must print same format, not touch backend."""

    def test_dry_run_no_backend_calls(self, tmp_path, monkeypatch):
        content = "dry run test content " * 100
        f = tmp_path / "dry.py"
        f.write_text(content)

        backend_calls = []
        def track_upsert(*args, **kwargs):
            backend_calls.append(True)

        mock_col = MagicMock()
        mock_col.upsert = track_upsert

        rooms = [{"name": "general", "keywords": []}]
        captured_output = []

        def fake_print(*args, **kwargs):
            captured_output.append(" ".join(str(a) for a in args))

        monkeypatch.setattr("builtins.print", fake_print)

        drawers, room = process_file(
            filepath=f,
            project_path=tmp_path,
            collection=mock_col,
            wing="wing",
            rooms=rooms,
            agent="test",
            dry_run=True,
        )

        assert drawers == len(chunk_text(content, str(f)))
        assert room == "general"
        assert len(backend_calls) == 0, "dry_run=True must not call upsert"


class TestBatchingBoundary:
    """Test batch filing behavior across files."""

    def test_two_files_two_upserts(self, tmp_path, monkeypatch):
        """Two files should produce exactly 2 upsert calls total."""
        f1 = tmp_path / "a.py"
        f1.write_text("a" * 1000)
        f2 = tmp_path / "b.py"
        f2.write_text("b" * 1000)

        upsert_calls = []
        def mock_upsert(documents, ids, metadatas):
            upsert_calls.append(len(documents))

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_col.upsert = mock_upsert

        rooms = [{"name": "general", "keywords": []}]

        process_file(f1, tmp_path, mock_col, "wing", rooms, "test", False)
        process_file(f2, tmp_path, mock_col, "wing", rooms, "test", False)

        assert len(upsert_calls) == 2, f"Expected 2 upserts, got {len(upsert_calls)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
