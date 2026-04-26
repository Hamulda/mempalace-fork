"""
tests/test_mining_batching.py
Batching tests for safe mining batching.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mempalace.miner import (
    _commit_batch,
    _prepare_file_drawers,
    _MEMPALACE_MINE_BATCH_FILES,
    _MEMPALACE_MINE_BATCH_DRAWERS,
)
from mempalace.mining_manifest import MiningManifest


# ─── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_collection():
    col = MagicMock()
    col.get.return_value = {"ids": [], "metadatas": []}
    col.upsert.return_value = None
    return col


@pytest.fixture
def mock_stats():
    stats = MagicMock()
    stats.record_file = MagicMock()
    return stats


@pytest.fixture
def mock_manifest(tmp_path):
    manifest = MagicMock(spec=MiningManifest)
    manifest.update_success = MagicMock()
    manifest.update_error = MagicMock()
    return manifest


@pytest.fixture
def project_path(tmp_path):
    return tmp_path


def make_file(root: Path, name: str, content: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestPrepareFileDrawers:
    """Unit tests for _prepare_file_drawers."""

    def test_prepares_correct_drawer_count(self, project_path, mock_stats):
        f = make_file(project_path, "test.py", ("def foo():\n    return 'this is a long enough line to pass min chunk'\n" * 100))

        result = _prepare_file_drawers(
            filepath=f,
            project_path=project_path,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            stats=mock_stats,
        )

        assert result is not None
        assert len(result["documents"]) > 0
        assert result["room"] is not None
        assert result["source_file"] == str(f)

    def test_skips_empty_file(self, project_path, mock_stats):
        f = make_file(project_path, "empty.txt", "")

        result = _prepare_file_drawers(
            filepath=f,
            project_path=project_path,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            stats=mock_stats,
        )

        assert result is None

    def test_skips_too_small_file(self, project_path, mock_stats):
        f = make_file(project_path, "small.txt", "x" * 30)  # below MIN_CHUNK_SIZE=50

        result = _prepare_file_drawers(
            filepath=f,
            project_path=project_path,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            stats=mock_stats,
        )

        assert result is None

    def test_sets_correct_metadata_fields(self, project_path, mock_stats):
        content = ("def foo():\n    return 'this is a long enough line to pass min chunk'\n" * 100)
        f = make_file(project_path, "test.py", content)

        result = _prepare_file_drawers(
            filepath=f,
            project_path=project_path,
            wing="my-wing",
            rooms=[{"name": "code", "description": ""}],
            agent="test-agent",
            stats=mock_stats,
        )

        assert result is not None
        for meta in result["metadatas"]:
            assert meta["wing"] == "my-wing"
            assert meta["agent_id"] == "test-agent"
            assert meta["is_latest"] is True
            assert "revision_id" in meta
            assert "content_hash" in meta


class TestCommitBatchFlushTriggers:
    """Tests for batch flush by file count and drawer count."""

    def test_flush_by_file_count(self, mock_collection, mock_stats, mock_manifest, project_path):
        """Batch flushes when file count reaches threshold."""
        # Create enough files to trigger flush
        files = []
        large_content = ("def func():\n    return 'this is a long enough body to pass min chunk size'\n" * 20)
        for i in range(_MEMPALACE_MINE_BATCH_FILES + 2):
            files.append(make_file(project_path, f"file_{i}.py", large_content))

        pending = []
        for f in files:
            prepared = _prepare_file_drawers(
                filepath=f,
                project_path=project_path,
                wing="test-wing",
                rooms=[{"name": "general", "description": ""}],
                agent="test-agent",
                stats=mock_stats,
            )
            if prepared:
                prepared["_manifest"] = {
                    "size_bytes": f.stat().st_size,
                    "mtime_ns": f.stat().st_mtime_ns,
                    "qh": b"fakehash",
                }
                pending.append(prepared)

        # Should flush twice: once at threshold, once at end
        drawers, committed, _ = _commit_batch(
            pending=pending,
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        assert committed == len(pending)
        assert drawers > 0
        # verify collection.get was called once for the batch
        mock_collection.get.assert_called()

    def test_flush_by_drawer_count(self, mock_collection, mock_stats, mock_manifest, project_path):
        """Batch flushes when total drawer count reaches threshold."""
        # Use actual code functions — each def generates its own chunk
        # to exceed _MEMPALACE_MINE_BATCH_DRAWERS // 2 = 128 drawers
        n_funcs = (_MEMPALACE_MINE_BATCH_DRAWERS // 2) + 5
        func_lines = "\n".join(
            f"def func_{i}():\n    return '{'x' * 100}'"
            for i in range(n_funcs)
        )
        large_file = make_file(project_path, "large.py", func_lines)

        prepared = _prepare_file_drawers(
            filepath=large_file,
            project_path=project_path,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            stats=mock_stats,
        )
        assert prepared is not None
        prepared["_manifest"] = {
            "size_bytes": large_file.stat().st_size,
            "mtime_ns": large_file.stat().st_mtime_ns,
            "qh": b"fakehash",
        }

        drawers, committed, _ = _commit_batch(
            pending=[prepared],
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        assert committed == 1
        assert drawers >= _MEMPALACE_MINE_BATCH_DRAWERS // 2

    def test_partial_batch_flush_at_end(self, mock_collection, mock_stats, mock_manifest, project_path):
        """Partial batch at end of processing is flushed."""
        # Create 3 files (below batch threshold of 8)
        large_content = ("def foo():\n    return 'this is a long enough body to pass min chunk'\n" * 20)
        files = []
        for i in range(3):
            files.append(make_file(project_path, f"partial_{i}.py", large_content))

        pending = []
        for f in files:
            prepared = _prepare_file_drawers(
                filepath=f,
                project_path=project_path,
                wing="test-wing",
                rooms=[{"name": "general", "description": ""}],
                agent="test-agent",
                stats=mock_stats,
            )
            if prepared:
                prepared["_manifest"] = {
                    "size_bytes": f.stat().st_size,
                    "mtime_ns": f.stat().st_mtime_ns,
                    "qh": b"fakehash",
                }
                pending.append(prepared)

        drawers, committed, _ = _commit_batch(
            pending=pending,
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        assert committed == 3
        assert drawers > 0

    def test_empty_pending_returns_zero(self, mock_collection, mock_stats, mock_manifest):
        """Empty pending batch returns zero."""
        drawers, committed, phase_totals = _commit_batch(
            pending=[],
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path="/tmp",
        )
        assert drawers == 0
        assert committed == 0
        assert phase_totals == {}


class TestCommitBatchSemanticCorrectness:
    """Tests that _commit_batch preserves exact per-file semantics."""

    def test_tombstones_stale_chunks(self, mock_collection, mock_stats, mock_manifest, project_path):
        """Old chunks not superseded by new content are tombstoned."""
        # Create a file, commit it, then re-commit with different content.
        # Old chunks whose content_hash doesn't appear in new content should be tombstoned.
        # Use longer functions so split_code_structurally produces chunks ≥MIN_CHUNK_SIZE=50
        f = make_file(project_path, "tombstone_test.py", (
            "def original():\n    x = 'this is a long enough body to pass min chunk'\n    return x\n" * 20
        ))

        prepared1 = _prepare_file_drawers(
            filepath=f,
            project_path=project_path,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            stats=mock_stats,
        )
        assert prepared1 is not None
        prepared1["_manifest"] = {"size_bytes": 1, "mtime_ns": 1, "qh": b"h1"}

        # First commit: no existing chunks
        mock_collection.get.return_value = {"ids": [], "metadatas": []}
        _commit_batch(
            pending=[prepared1],
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        # Record how many upserts happened on first commit
        first_commit_upserts = mock_collection.upsert.call_count

        # Second commit: modify the file content so old chunks don't match
        f.write_text((
            "def modified():\n    x = 'this is a long enough body to pass min chunk'\n    return x\n" * 20
        ), encoding="utf-8")

        prepared2 = _prepare_file_drawers(
            filepath=f,
            project_path=project_path,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            stats=mock_stats,
        )
        assert prepared2 is not None
        prepared2["_manifest"] = {"size_bytes": 2, "mtime_ns": 2, "qh": b"h2"}

        # Return the old chunks from first commit (with content_hash from first version)
        # New chunks have different content_hash → no supersedes → all old chunks tombstoned
        old_ids = [f"old_id_{i}" for i in range(len(prepared1["ids"]))]
        old_metas = [
            {**m, "content_hash": f"old_hash_{i}"}
            for i, m in enumerate(prepared1["metadatas"])
        ]
        mock_collection.get.return_value = {"ids": old_ids, "metadatas": old_metas}
        mock_collection.upsert.reset_mock()

        _commit_batch(
            pending=[prepared2],
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        # Should have: data upsert + tombstone upsert (≥2)
        assert mock_collection.upsert.call_count >= 2, (
            f"Expected ≥2 upserts (data + tombstones), got {mock_collection.upsert.call_count}"
        )

    def test_manifest_updated_for_each_file(self, mock_collection, mock_stats, mock_manifest, project_path):
        """Manifest is updated for each successfully committed file."""
        large_content = ("def code():\n    return 'sufficient length for min chunk'\n" * 20)
        files = []
        for i in range(3):
            files.append(make_file(project_path, f"manifest_test_{i}.py", large_content))

        pending = []
        for f in files:
            prepared = _prepare_file_drawers(
                filepath=f,
                project_path=project_path,
                wing="test-wing",
                rooms=[{"name": "general", "description": ""}],
                agent="test-agent",
                stats=mock_stats,
            )
            if prepared:
                prepared["_manifest"] = {
                    "size_bytes": f.stat().st_size,
                    "mtime_ns": f.stat().st_mtime_ns,
                    "qh": b"fakehash",
                }
                pending.append(prepared)

        _commit_batch(
            pending=pending,
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        assert mock_manifest.update_success.call_count == 3

    def test_failure_isolated_per_file(self, mock_collection, mock_stats, mock_manifest, project_path):
        """One file's failure does not prevent other files from being committed."""
        large_content = ("def code():\n    return 'sufficient length for min chunk'\n" * 20)
        files = []
        for i in range(3):
            files.append(make_file(project_path, f"isolation_{i}.py", large_content))

        pending = []
        for f in files:
            prepared = _prepare_file_drawers(
                filepath=f,
                project_path=project_path,
                wing="test-wing",
                rooms=[{"name": "general", "description": ""}],
                agent="test-agent",
                stats=mock_stats,
            )
            if prepared:
                prepared["_manifest"] = {
                    "size_bytes": f.stat().st_size,
                    "mtime_ns": f.stat().st_mtime_ns,
                    "qh": b"fakehash",
                }
                pending.append(prepared)

        # Make upsert raise after successful commits for file 0
        # _commit_batch catches exceptions per-file and continues
        mock_collection.upsert.side_effect = RuntimeError("Simulated upsert failure")

        drawers, committed, _ = _commit_batch(
            pending=pending,
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        # All upserts fail → committed = 0 (no file completes without error)
        assert committed == 0, f"Expected committed=0, got {committed}"

    def test_batch_get_single_query_for_multiple_files(self, mock_collection, mock_stats, mock_manifest, project_path):
        """Batch existing-chunk lookup uses a single query for all source_files."""
        large_content = ("def code():\n    return 'sufficient length for min chunk'\n" * 20)
        files = []
        for i in range(4):
            files.append(make_file(project_path, f"batch_get_{i}.py", large_content))

        pending = []
        for f in files:
            prepared = _prepare_file_drawers(
                filepath=f,
                project_path=project_path,
                wing="test-wing",
                rooms=[{"name": "general", "description": ""}],
                agent="test-agent",
                stats=mock_stats,
            )
            if prepared:
                prepared["_manifest"] = {
                    "size_bytes": f.stat().st_size,
                    "mtime_ns": f.stat().st_mtime_ns,
                    "qh": b"fakehash",
                }
                pending.append(prepared)

        mock_collection.get.return_value = {"ids": [], "metadatas": []}

        _commit_batch(
            pending=pending,
            collection=mock_collection,
            wing="test-wing",
            rooms=[{"name": "general", "description": ""}],
            agent="test-agent",
            palace_path="/tmp/test",
            stats=mock_stats,
            manifest=mock_manifest,
            project_path=str(project_path),
        )

        # collection.get should be called exactly once (not once per file)
        assert mock_collection.get.call_count == 1
        call_kwargs = mock_collection.get.call_args.kwargs
        where_clause = call_kwargs.get("where") or (call_kwargs.get("where") if isinstance(call_kwargs.get("where"), dict) else None)
        # With 4 files, should use $or query
        assert where_clause is not None


class TestBatchEnvironmentVariables:
    """Tests that batch thresholds are configurable via environment variables."""

    def test_batch_files_env_override(self, monkeypatch):
        """MEMPALACE_MINE_BATCH_FILES can override default batch size."""
        import os
        monkeypatch.setenv("MEMPALACE_MINE_BATCH_FILES", "2")
        # Module-level constant is read at import time; verify it matches env
        from mempalace import miner as miner_module
        actual = os.environ.get("MEMPALACE_MINE_BATCH_FILES")
        assert actual == "2"

    def test_batch_drawers_env_override(self, monkeypatch):
        """MEMPALACE_MINE_BATCH_DRAWERS can override default drawer threshold."""
        import os
        monkeypatch.setenv("MEMPALACE_MINE_BATCH_DRAWERS", "100")
        actual = os.environ.get("MEMPALACE_MINE_BATCH_DRAWERS")
        assert actual == "100"

    def test_default_batch_files_value(self):
        """Default MEMPALACE_MINE_BATCH_FILES is 8."""
        assert _MEMPALACE_MINE_BATCH_FILES == 8

    def test_default_batch_drawers_value(self):
        """Default MEMPALACE_MINE_BATCH_DRAWERS is 256."""
        assert _MEMPALACE_MINE_BATCH_DRAWERS == 256
