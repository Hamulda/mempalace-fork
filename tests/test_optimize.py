"""
Tests for LanceOptimizer background compaction.

Run: pytest tests/test_optimize.py -v -s
"""

import os
import tempfile
import time as _time_mod
from pathlib import Path

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed — run: pip install 'mempalace[lance]'")

from mempalace.backends.lance import LanceOptimizer, LanceBackend


@pytest.fixture
def tmp_palace():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


class TestLanceOptimizer:
    def test_optimize_lock_prevents_concurrent(self, tmp_palace):
        """Lock file prevents concurrent optimize runs."""
        opt = LanceOptimizer(tmp_palace, "test_col")

        # Create a stale lock file
        opt._lock_file.touch()
        assert opt._lock_file.exists() is True

        # _run_optimize should bail out when lock exists
        opt._run_optimize()

        # Lock file should still exist after bail-out
        assert opt._lock_file.exists() is True

        # Clean up
        opt._lock_file.unlink(missing_ok=True)

    def test_palace_path_required_for_optimizer(self, tmp_palace):
        """Optimizer stores palace_path and lock file location."""
        opt = LanceOptimizer(tmp_palace, "optim_test")
        assert opt._palace_path == tmp_palace
        assert opt._collection_name == "optim_test"
        assert opt._lock_file == Path(tmp_palace) / ".optimize_lock"

    def test_record_write_counts_writes(self, tmp_palace):
        """record_write() increments internal counter without triggering optimize."""
        opt = LanceOptimizer(tmp_palace, "test_col_write")
        # High thresholds so optimize never triggers during this test
        opt.OPTIMIZE_WRITES_THRESHOLD = 9999
        opt.OPTIMIZE_INTERVAL_SECONDS = 9999
        # Reset counter from any previous runs
        opt._writes_since_optimize = 0
        opt._last_optimize_time = _time_mod.monotonic()

        for i in range(10):
            opt.record_write()

        assert opt._writes_since_optimize == 10, f"Expected 10, got {opt._writes_since_optimize}"

    def test_run_optimize_sync_works(self, tmp_palace):
        """run_optimize_sync() executes without error when collection exists."""
        from mempalace.backends.lance import LanceBackend

        backend = LanceBackend()
        col = backend.get_collection(tmp_palace, "sync_test", create=True)
        col.add(
            documents=["test doc for optimize"],
            ids=["opt_doc_1"],
            metadatas=[{"wing": "test"}],
        )

        # This should not raise
        col.run_optimize()


class TestIndexCleanup:
    def test_index_cleanup_removes_old_dirs(self, tmp_palace):
        """LanceOptimizer keeps only 2 newest _indices directories."""
        # Create the _indices directory structure manually (avoid embedding calls)
        table_dir = Path(tmp_palace) / "cleanup_test.lance" / "_indices"
        table_dir.mkdir(parents=True, exist_ok=True)

        # Create 5 fake index dirs with different ages
        fake_dirs = []
        for i in range(5):
            d = table_dir / f"fake_index_{i}"
            d.mkdir(exist_ok=True)
            fake_dirs.append(d)

        # Set mtimes: oldest first using os.utime
        now = _time_mod.time()
        for i, d in enumerate(fake_dirs):
            _time_mod.sleep(0.01)
            os.utime(d, (now - i * 100, now - i * 100))

        # Run optimizer which should clean up all but 2 newest
        opt = LanceOptimizer(tmp_palace, "cleanup_test")
        opt._run_optimize()

        remaining = [d for d in table_dir.iterdir() if d.is_dir()]
        assert len(remaining) <= 2, f"Expected <=2 dirs, got {len(remaining)}: {remaining}"

    def test_empty_index_dirs_removed(self, tmp_palace):
        """Empty _indices subdirectories are removed by optimizer."""
        table_dir = Path(tmp_palace) / "empty_cleanup_test.lance" / "_indices"
        table_dir.mkdir(parents=True, exist_ok=True)

        # Create empty dir
        empty_dir = table_dir / "empty_index"
        empty_dir.mkdir(exist_ok=True)

        assert empty_dir.exists()

        opt = LanceOptimizer(tmp_palace, "empty_cleanup_test")
        opt._run_optimize()

        # Empty dir should be removed
        assert not empty_dir.exists(), "Empty index dir should have been removed"
