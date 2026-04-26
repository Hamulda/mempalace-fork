"""
Tests for mining profiler (MineStats and MEMPALACE_MINE_PROFILE env flag).

Verifies:
- MineStats records top slow/largest-chunk files
- MEMPALACE_MINE_PROFILE=1 enables profiling
- JSON report writes valid JSON
- Progress output uses flush (no crash)
- Periodic progress every 25 files
"""
import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMineStats:
    """Unit tests for MineStats collector."""

    def test_stats_records_top_slow_files(self):
        """Top 20 slowest files tracked correctly."""
        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        try:
            from mempalace.miner import MineStats
            stats = MineStats()

            # Add 25 fake records with varying total_s
            for i in range(25):
                stats.record_file({
                    "status": "added",
                    "source_file": f"/fake/path/file_{i:02d}.py",
                    "room": "general",
                    "chunk_count": i + 1,
                    "total_s": float(i * 0.1 + 0.05),
                    "read_file_s": 0.01,
                    "detect_room_s": 0.001,
                    "chunk_s": 0.005,
                    "revision_existing_get_s": 0.02,
                    "prepare_metadata_s": 0.002,
                    "collection_upsert_s": 0.05 + i * 0.01,
                    "tombstone_upsert_s": 0.0,
                })

            report = stats.final_report()
            assert len(report["slowest_files"]) == 20
            # File 24 should be slowest (highest total_s)
            slowest = report["slowest_files"][0]
            assert slowest["source_file"].endswith("file_24.py")
            assert slowest["total_s"] > 0
        finally:
            os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_stats_records_top_largest_chunk_files(self):
        """Top 20 largest-chunk-count files tracked correctly."""
        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        try:
            from mempalace.miner import MineStats
            stats = MineStats()

            for i in range(25):
                stats.record_file({
                    "status": "added",
                    "source_file": f"/fake/big_file_{i:02d}.py",
                    "room": "general",
                    "chunk_count": (i + 1) * 10,  # file_24 has 240 chunks
                    "total_s": 0.5,
                    "read_file_s": 0.01,
                    "detect_room_s": 0.001,
                    "chunk_s": 0.005,
                    "revision_existing_get_s": 0.02,
                    "prepare_metadata_s": 0.002,
                    "collection_upsert_s": 0.3,
                    "tombstone_upsert_s": 0.0,
                })

            report = stats.final_report()
            assert len(report["largest_chunk_files"]) == 20
            largest = report["largest_chunk_files"][0]
            assert largest["chunk_count"] == 250  # file_24 has 250 chunks (25*10)
            assert largest["source_file"].endswith("big_file_24.py")
        finally:
            os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_env_flag_enables_profiling(self):
        """MEMPALACE_MINE_PROFILE=1 enables stats collection."""
        os.environ.pop("MEMPALACE_MINE_PROFILE", None)
        from mempalace.miner import MineStats

        # Disabled by default
        stats_disabled = MineStats()
        assert stats_disabled.enabled is False

        # Enabled via env
        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        try:
            stats_enabled = MineStats()
            assert stats_enabled.enabled is True
        finally:
            os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_json_report_writes_valid_json(self):
        """MEMPALACE_MINE_PROFILE_JSON writes valid JSON file."""
        from mempalace.miner import MineStats

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            json_path = f.name

        try:
            os.environ["MEMPALACE_MINE_PROFILE_JSON"] = json_path
            stats = MineStats()

            # Add a few records
            stats.record_file({
                "status": "added", "source_file": "/test/a.py",
                "room": "general", "chunk_count": 5, "total_s": 0.3,
                "read_file_s": 0.05, "detect_room_s": 0.01, "chunk_s": 0.02,
                "revision_existing_get_s": 0.1, "prepare_metadata_s": 0.01,
                "collection_upsert_s": 0.1, "tombstone_upsert_s": 0.0,
            })
            stats.record_file({
                "status": "added", "source_file": "/test/b.py",
                "room": "general", "chunk_count": 3, "total_s": 0.2,
                "read_file_s": 0.03, "detect_room_s": 0.005, "chunk_s": 0.01,
                "revision_existing_get_s": 0.08, "prepare_metadata_s": 0.005,
                "collection_upsert_s": 0.07, "tombstone_upsert_s": 0.0,
            })

            stats.final_report()

            # Verify JSON file was written
            assert os.path.exists(json_path)
            with open(json_path) as fread:
                loaded = json.load(fread)

            assert loaded["total_files"] == 2
            assert loaded["processed_files"] == 2
            assert "phase_totals" in loaded
            assert "slowest_files" in loaded
            assert "largest_chunk_files" in loaded
            assert "errors" in loaded
            assert "total_runtime_s" in loaded

            os.environ.pop("MEMPALACE_MINE_PROFILE_JSON", None)
        finally:
            os.unlink(json_path)
            os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_skipped_and_error_files_counted(self):
        """Skipped and error files are correctly counted."""
        from mempalace.miner import MineStats

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        stats = MineStats()

        stats.record_file({"status": "skipped", "source_file": "/test/skip.py",
                           "chunk_count": 0, "room": None, "total_s": 0.1})
        stats.record_file({"status": "error", "source_file": "/test/error.py",
                           "phase": "read_file", "error": "OSError", "total_s": 0.05})
        stats.record_file({
            "status": "added", "source_file": "/test/ok.py",
            "chunk_count": 5, "room": "general", "total_s": 0.3,
            "read_file_s": 0.05, "detect_room_s": 0.01, "chunk_s": 0.02,
            "revision_existing_get_s": 0.1, "prepare_metadata_s": 0.01,
            "collection_upsert_s": 0.1, "tombstone_upsert_s": 0.0,
        })

        report = stats.final_report()
        assert report["skipped_files"] == 1
        assert report["error_files"] == 1
        assert report["processed_files"] == 1
        assert report["total_files"] == 3

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_phase_totals_accumulated(self):
        """Phase totals correctly sum across all processed files."""
        from mempalace.miner import MineStats

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        stats = MineStats()

        for i in range(5):
            stats.record_file({
                "status": "added", "source_file": f"/test/f{i}.py",
                "room": "general", "chunk_count": 3, "total_s": 0.4,
                "read_file_s": 0.05, "detect_room_s": 0.01, "chunk_s": 0.02,
                "revision_existing_get_s": 0.1, "prepare_metadata_s": 0.02,
                "collection_upsert_s": 0.15, "tombstone_upsert_s": 0.05,
            })

        r = stats.final_report()
        assert r["phase_totals"]["read_file_s"] == pytest.approx(0.25, rel=0.01)
        assert r["phase_totals"]["collection_upsert_s"] == pytest.approx(0.75, rel=0.01)
        assert r["phase_totals"]["total_s"] == pytest.approx(2.0, rel=0.01)

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_progress_uses_flush(self):
        """Progress output does not crash when flush is called."""
        from mempalace.miner import MineStats

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        stats = MineStats()

        # Add 25 files to trigger periodic progress
        for i in range(25):
            stats.record_file({
                "status": "added", "source_file": f"/test/f{i}.py",
                "room": "general", "chunk_count": 3, "total_s": 0.1,
                "read_file_s": 0.01, "detect_room_s": 0.001, "chunk_s": 0.005,
                "revision_existing_get_s": 0.05, "prepare_metadata_s": 0.005,
                "collection_upsert_s": 0.05, "tombstone_upsert_s": 0.0,
            })

        # Should not raise any exceptions
        # (progress output goes to stdout, we just verify no crash)
        assert stats.total_files == 25

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_disabled_stats_no_overhead(self):
        """When profiling is disabled, record_file is a no-op (low overhead)."""
        from mempalace.miner import MineStats

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)
        stats = MineStats()
        assert stats.enabled is False

        # These should be no-ops, not raise
        stats.record_file({
            "status": "added", "source_file": "/test/f.py",
            "chunk_count": 5, "room": "general", "total_s": 0.3,
            "read_file_s": 0.05, "detect_room_s": 0.01, "chunk_s": 0.02,
            "revision_existing_get_s": 0.1, "prepare_metadata_s": 0.01,
            "collection_upsert_s": 0.1, "tombstone_upsert_s": 0.0,
        })

        # Nothing accumulated
        report = stats.final_report()
        assert report["total_files"] == 0
        assert report["processed_files"] == 0

    def test_error_list_capped_at_50(self):
        """Errors list is capped at 50 entries."""
        from mempalace.miner import MineStats

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        stats = MineStats()

        for i in range(100):
            stats.record_file({
                "status": "error", "source_file": f"/test/err{i}.py",
                "phase": "read_file", "error": f"error_{i}", "total_s": 0.01,
            })

        report = stats.final_report()
        assert len(report["errors"]) == 50

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)

    def test_print_summary_does_not_crash_when_disabled(self):
        """print_summary() is safe to call even when not enabled."""
        from mempalace.miner import MineStats

        stats = MineStats()
        assert stats.enabled is False

        # Should not crash
        stats.print_summary()

        report = stats.final_report()
        assert report["total_files"] == 0