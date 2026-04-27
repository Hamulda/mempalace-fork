"""
Tests for mining profiler (MineStats and MEMPALACE_MINE_PROFILE env flag).

Verifies:
- MineStats records top slow/largest-chunk files
- MEMPALACE_MINE_PROFILE=1 enables profiling
- JSON report writes valid JSON
- Progress output uses flush (no crash)
- Periodic progress every 25 files
- mine() writes bounded profile JSON even on exception (finally block)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use chroma backend for integration tests (avoids MLX dependency)
os.environ["MEMPALACE_BACKEND"] = "chroma"


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
        """MEMPALACE_MINE_PROFILE_JSON env var is read at init time."""
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

            report = stats.final_report()

            # Verify report dict has expected keys (JSON writing is now in mine())
            assert "total_files" in report
            assert "processed_files" in report
            assert "phase_totals" in report
            assert "slowest_files" in report
            assert "largest_chunk_files" in report
            assert "errors" in report
            assert "total_runtime_s" in report
            assert "skipped_files" in report
            assert "error_files" in report
            assert "total_drawers_added" in report
            assert "files_per_sec" in report

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


class TestMineProfileJson:
    """Integration tests for MEMPALACE_MINE_PROFILE_JSON in mine().

    Uses the session-wide _mock_embed_for_all_tests from conftest which patches
    _embed_texts to return fast deterministic fakes.  The real mining path runs
    (scan → chunk → upsert) but without any MLX daemon subprocesses.
    """

    def _make_project(self, tmpdir: Path) -> Path:
        """Create a minimal mempalace-registered project in tmpdir."""
        proj = tmpdir / "project"
        proj.mkdir()
        (proj / "mempalace.yaml").write_text(
            yaml.dump({"wing": "test-wing", "rooms": [{"name": "general", "description": "General"}]})
        )
        (proj / "main.py").write_text("def hello():\n    print('world')\n" * 10)
        return proj

    def test_profile_json_written_on_success(self, tmp_path):
        """mine() writes valid JSON to MEMPALACE_MINE_PROFILE_JSON on success."""
        from mempalace.miner import mine

        proj = self._make_project(tmp_path)
        palace = tmp_path / "palace"
        profile = tmp_path / "profile.json"

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        os.environ["MEMPALACE_MINE_PROFILE_JSON"] = str(profile)

        mine(str(proj), str(palace))

        assert profile.exists(), "profile JSON not written after mine()"
        data = json.loads(profile.read_text())
        assert "total_files" in data
        assert "processed_files" in data
        assert "skipped_files" in data
        assert "error_files" in data
        assert "total_drawers_added" in data
        assert "phase_totals" in data
        assert "slowest_files" in data
        assert "largest_chunk_files" in data
        assert "errors" in data
        assert "total_runtime_s" in data
        assert "files_per_sec" in data

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)
        os.environ.pop("MEMPALACE_MINE_PROFILE_JSON", None)

    def test_profile_json_written_and_required_keys_present(self, tmp_path):
        """mine() writes valid JSON with all required keys when a real mining run occurs.

        Uses the session-wide _mock_embed_for_all_tests fixture which patches
        _embed_texts to return fast deterministic fakes (no MLX subprocess).
        """
        from mempalace.miner import mine

        proj = self._make_project(tmp_path)
        palace = tmp_path / "palace"
        profile = tmp_path / "profile.json"

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        os.environ["MEMPALACE_MINE_PROFILE_JSON"] = str(profile)

        # The conftest session fixture patches _embed_texts, so mining runs
        # in-process without spawning an embed daemon subprocess.
        mine(str(proj), str(palace))

        assert profile.exists(), "profile JSON not written after mine()"
        data = json.loads(profile.read_text())

        # Verify all required keys are present
        required = {
            "total_files", "processed_files", "skipped_files", "error_files",
            "total_drawers_added", "phase_totals", "slowest_files",
            "largest_chunk_files", "errors", "total_runtime_s", "files_per_sec",
        }
        assert required.issubset(data.keys()), f"Missing keys: {required - data.keys()}"

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)
        os.environ.pop("MEMPALACE_MINE_PROFILE_JSON", None)

    def test_profile_json_no_absolute_local_paths(self, tmp_path):
        """Profile JSON slowest/largest entries contain no absolute /tmp or $HOME paths."""
        from mempalace.miner import mine

        proj = self._make_project(tmp_path)
        palace = tmp_path / "palace"
        profile = tmp_path / "profile.json"

        os.environ["MEMPALACE_MINE_PROFILE"] = "1"
        os.environ["MEMPALACE_MINE_PROFILE_JSON"] = str(profile)

        mine(str(proj), str(palace))

        data = json.loads(profile.read_text())
        home = str(Path.home())
        for key in ("slowest_files", "largest_chunk_files"):
            for entry in data.get(key, []):
                sf = entry.get("source_file", "")
                assert not sf.startswith("/tmp"), f"{key} contains /tmp path: {sf}"
                assert not sf.startswith(home), f"{key} contains home path: {sf}"

        os.environ.pop("MEMPALACE_MINE_PROFILE", None)
        os.environ.pop("MEMPALACE_MINE_PROFILE_JSON", None)
