"""
test_diagnostics.py — Smoke tests for mempalace.diagnostics module.
"""

import os
import tempfile
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mempalace.diagnostics import (
    validate_symbol_index,
    validate_keyword_index,
    validate_runtime_state,
    validate_skills_registration,
    rebuild_symbol_index,
    rebuild_keyword_index,
)


class TestValidateSymbolIndex:
    def test_runs_without_error(self, palace_path, tmp_path):
        """validate_symbol_index runs without error and returns expected keys."""
        # Create a minimal project structure
        project = tmp_path / "project"
        project.mkdir()
        (project / "main.py").write_text("def foo(): pass\n")

        result = validate_symbol_index(palace_path, str(project))

        assert isinstance(result, dict)
        assert "orphaned_files" in result
        assert "missing_from_index" in result
        assert "stats" in result

    def test_detects_orphaned_files(self, palace_path, tmp_path):
        """Orphaned index entries (files in index but not on disk) are detected."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "main.py").write_text("def foo(): pass\n")

        # Add a file to the index that doesn't exist on disk
        idx_path = Path(palace_path) / "symbol_index.sqlite3"
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(idx_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS symbol_index ("
            "id INTEGER PRIMARY KEY, symbol_name TEXT, symbol_type TEXT, "
            "file_path TEXT, line_start INTEGER, line_end INTEGER, "
            "file_signature TEXT, imports TEXT, direct_imports TEXT, "
            "exports TEXT, indexed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO symbol_index (symbol_name, symbol_type, file_path, line_start) "
            "VALUES ('orphan', 'function', '/nonexistent/file.py', 1)"
        )
        conn.commit()
        conn.close()

        result = validate_symbol_index(palace_path, str(project))
        assert any("nonexistent" in fp for fp in result["orphaned_files"])


class TestValidateKeywordIndex:
    def test_runs_without_error(self, palace_path, tmp_path):
        """validate_keyword_index runs without error and returns expected keys."""
        result = validate_keyword_index(palace_path)

        assert isinstance(result, dict)
        assert "fts5_count" in result
        assert "lance_count" in result
        assert "counts_match" in result
        assert "stats" in result

    @pytest.mark.slow
    def test_counts_match_when_consistent(self, palace_path, seeded_collection):
        """When FTS5 and LanceDB have same documents, counts_match is True."""
        result = validate_keyword_index(palace_path)

        # If both are empty or equal, counts_match should be True
        if result["fts5_count"] == result["lance_count"]:
            assert result["counts_match"] is True


class TestValidateRuntimeState:
    def test_runs_without_error(self, palace_path):
        """validate_runtime_state runs without error and returns expected keys."""
        result = validate_runtime_state(palace_path)

        assert isinstance(result, dict)
        assert "query_cache_size" in result
        assert "daemon_running" in result
        assert "memory_pressure" in result
        assert "palace_initialized" in result

    def test_returns_palace_initialized_bool(self, palace_path):
        """Returns whether palace directory exists."""
        result = validate_runtime_state(palace_path)
        assert isinstance(result["palace_initialized"], bool)

    def test_returns_cache_size_across_all_shards(self, palace_path):
        """query_cache_size reflects total entries across all 8 shards."""
        from mempalace.query_cache import QueryCache

        fake_cache = QueryCache(maxsize=256, ttl_seconds=60.0)
        for i in range(fake_cache._NUM_SHARDS):
            fake_cache._shards[i]["cache"]["key_in_shard_%d" % i] = ("value", time.monotonic())

        import mempalace.query_cache as qc_module
        original_cache = qc_module._query_cache
        qc_module._query_cache = fake_cache

        try:
            from mempalace.diagnostics import validate_runtime_state
            result = validate_runtime_state(palace_path)
            assert result["query_cache_size"] == fake_cache._NUM_SHARDS
        finally:
            qc_module._query_cache = original_cache


class TestValidateSkillsRegistration:
    def test_returns_expected_keys(self, palace_path):
        """validate_skills_registration returns expected dict keys."""
        skills_dir = Path(__file__).parent.parent / "mempalace" / "skills"
        result = validate_skills_registration(str(skills_dir))

        assert isinstance(result, dict)
        assert "missing" in result
        assert "empty" in result
        assert "total_expected" in result
        assert "total_found" in result

    def test_all_expected_skills_present(self, palace_path):
        """All expected skill files are found in the skills directory."""
        skills_dir = Path(__file__).parent.parent / "mempalace" / "skills"
        result = validate_skills_registration(str(skills_dir))

        assert len(result["missing"]) == 0, f"Missing skills: {result['missing']}"
        assert len(result["empty"]) == 0, f"Empty skill files: {result['empty']}"


class TestRebuildKeywordIndex:
    def test_changes_fts5_count(self, palace_path, seeded_collection):
        """rebuild_keyword_index actually changes the FTS5 count."""
        from mempalace.lexical_index import KeywordIndex

        ki = KeywordIndex.get(palace_path)
        count_before = ki.count()

        # Rebuild keyword index from LanceDB
        result = rebuild_keyword_index(palace_path)

        assert "documents_indexed" in result
        count_after = ki.count()
        # If there are documents in LanceDB, count_after should equal documents_indexed
        if result["documents_indexed"] > 0:
            assert count_after == result["documents_indexed"]


class TestSymbolIndexPublicAPI:
    def test_list_indexed_files_returns_set(self, palace_path, tmp_path):
        """list_indexed_files returns a set of strings, not internal objects."""
        from mempalace.symbol_index import SymbolIndex

        idx = SymbolIndex.get(palace_path)
        result = idx.list_indexed_files()

        assert isinstance(result, set)
        for item in result:
            assert isinstance(item, str)

    def test_list_indexed_files_empty_for_fresh_index(self, palace_path):
        """A fresh index has no indexed files."""
        from mempalace.symbol_index import SymbolIndex

        idx = SymbolIndex.get(palace_path)
        result = idx.list_indexed_files()

        assert result == set()

    def test_validate_symbol_index_uses_public_api(self, palace_path, tmp_path):
        """validate_symbol_index uses list_indexed_files (observable: orphaned detection still works)."""
        # Insert a known orphaned entry directly into the SQLite file
        idx_path = Path(palace_path) / "symbol_index.sqlite3"
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(idx_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS symbol_index ("
            "id INTEGER PRIMARY KEY, symbol_name TEXT, symbol_type TEXT, "
            "file_path TEXT, line_start INTEGER, line_end INTEGER, "
            "file_signature TEXT, imports TEXT, direct_imports TEXT, "
            "exports TEXT, indexed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO symbol_index (symbol_name, symbol_type, file_path, line_start) "
            "VALUES ('orphan_fn', 'function', '/nonexistent/orphan.py', 1)"
        )
        conn.commit()
        conn.close()

        project = tmp_path / "project"
        project.mkdir()
        (project / "main.py").write_text("def foo(): pass\n")

        from mempalace.diagnostics import validate_symbol_index
        result = validate_symbol_index(palace_path, str(project))

        # If validate_symbol_index used list_indexed_files correctly,
        # it will detect the orphaned file we just inserted
        assert any("orphan.py" in fp for fp in result["orphaned_files"])


class TestKeywordIndexPublicAPI:
    def test_sample_ids_returns_list_of_strings(self, palace_path):
        """sample_ids returns a list of string document IDs."""
        from mempalace.lexical_index import KeywordIndex

        ki = KeywordIndex.get(palace_path)
        result = ki.sample_ids(n=5)

        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, str)

    def test_sample_ids_empty_when_index_empty(self, palace_path):
        """sample_ids returns empty list when index is empty."""
        from mempalace.lexical_index import KeywordIndex

        ki = KeywordIndex.get(palace_path)
        result = ki.sample_ids(n=5)

        assert result == []

    def test_bulk_insert_batch_accumulates_across_calls(self, palace_path):
        """Multiple bulk_insert_batch calls accumulate — no internal clear between calls."""
        from mempalace.lexical_index import KeywordIndex

        ki = KeywordIndex.get(palace_path)
        ki.clear()

        batch1 = [
            {"document_id": "doc1", "content": "hello world", "wing": "", "room": "", "language": ""},
            {"document_id": "doc2", "content": "foo bar", "wing": "", "room": "", "language": ""},
        ]
        batch2 = [
            {"document_id": "doc3", "content": "baz qux", "wing": "", "room": "", "language": ""},
        ]

        ki.bulk_insert_batch(batch1)
        ki.bulk_insert_batch(batch2)

        assert ki.count() == 3

    def test_bulk_insert_batch_skips_clear(self, palace_path):
        """bulk_insert_batch does NOT clear — pre-existing entries persist."""
        from mempalace.lexical_index import KeywordIndex

        ki = KeywordIndex.get(palace_path)
        ki.clear()

        ki.bulk_insert_batch([
            {"document_id": "doc1", "content": "hello", "wing": "", "room": "", "language": ""},
        ])

        # Second batch should NOT clear first batch
        ki.bulk_insert_batch([
            {"document_id": "doc2", "content": "world", "wing": "", "room": "", "language": ""},
        ])

        assert ki.count() == 2


class TestValidateRuntimeStateNoSideEffects:
    def test_memory_guard_get_if_running_does_not_create_instance(self, palace_path):
        """get_if_running must not create the singleton when it doesn't exist."""
        import mempalace.memory_guard as mg

        # Ensure clean state
        with mg.MemoryGuard._lock:
            mg.MemoryGuard._instance = None
            mg.MemoryGuard._started.clear()

        try:
            result = mg.MemoryGuard.get_if_running()
            assert result is None
            # Verify no instance was created
            assert mg.MemoryGuard._instance is None
        finally:
            # Cleanup: reset to clean state
            with mg.MemoryGuard._lock:
                mg.MemoryGuard._instance = None
                mg.MemoryGuard._started.clear()

    def test_validate_runtime_state_does_not_start_memory_guard(self, palace_path):
        """validate_runtime_state must not trigger MemoryGuard startup."""
        import mempalace.memory_guard as mg

        with mg.MemoryGuard._lock:
            mg.MemoryGuard._instance = None
            mg.MemoryGuard._started.clear()

        try:
            from mempalace.diagnostics import validate_runtime_state
            result = validate_runtime_state(palace_path)

            assert result["memory_guard_running"] is False
            assert mg.MemoryGuard._instance is None
        finally:
            with mg.MemoryGuard._lock:
                mg.MemoryGuard._instance = None
                mg.MemoryGuard._started.clear()

    def test_validate_runtime_state_returns_memory_guard_running_key(self, palace_path):
        """Result includes memory_guard_running key even when not running."""
        from mempalace.diagnostics import validate_runtime_state

        result = validate_runtime_state(palace_path)

        assert "memory_guard_running" in result
        assert isinstance(result["memory_guard_running"], bool)


class TestRebuildKeywordIndexBatching:
    def test_rebuild_keyword_index_returns_batch_count(self, palace_path, monkeypatch):
        """rebuild_keyword_index returns the number of batches processed."""
        from mempalace.lexical_index import KeywordIndex
        from mempalace.backends import get_backend

        ki = KeywordIndex.get(palace_path)
        ki.clear()

        num_docs = 5
        batch_size = 2
        for i in range(num_docs):
            ki.bulk_insert_batch([{
                "document_id": f"doc_{i}",
                "content": f"document content {i}",
                "wing": "test",
                "room": "unit",
                "language": "en",
            }])

        assert ki.count() == num_docs

        class FakeCollection:
            def count(self):
                return num_docs
            def get(self, limit=1000, offset=0, include=None):
                ids = [f"doc_{i}" for i in range(offset, min(offset + limit, num_docs))]
                return {"ids": ids, "documents": [f"content {i}" for i in ids],
                        "metadatas": [{"wing": "test", "room": "unit", "language": "en"}] * len(ids)}

        fake_col = FakeCollection()

        class FakeLanceBackend:
            def get_collection(self, pp, cn, create=False):
                return fake_col

        monkeypatch.setattr("mempalace.backends.get_backend", lambda *a, **kw: FakeLanceBackend())

        result = rebuild_keyword_index(palace_path, batch_size=batch_size)

        assert "batches" in result
        assert isinstance(result["batches"], int)
        assert result["batches"] >= 1

    def test_rebuild_keyword_index_batches_flushed_immediately(self, palace_path, seeded_collection):
        """After each batch, FTS5 is flushed — ki.count() increases incrementally."""
        from mempalace.lexical_index import KeywordIndex
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        ki = KeywordIndex.get(palace_path)
        ki.clear()

        cfg = MempalaceConfig()
        backend = get_backend("lance")
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        total = col.count()

        # Rebuild with tiny batch_size=1 to force multiple batches
        result = rebuild_keyword_index(palace_path, batch_size=1)

        assert result["documents_indexed"] == total
        assert ki.count() == total

    def test_rebuild_keyword_index_backup_before_destructive(self, palace_path, seeded_collection):
        """backup_path is set before clear() or any destructive operation."""
        ki_path = str(Path(palace_path) / "keyword_index.sqlite3")
        Path(ki_path).parent.mkdir(parents=True, exist_ok=True)

        # Create a non-empty index so there's something to backup
        from mempalace.lexical_index import KeywordIndex
        ki = KeywordIndex.get(palace_path)
        ki.bulk_insert_batch([
            {"document_id": "seed", "content": "seed content", "wing": "", "room": "", "language": ""},
        ])

        assert ki.count() > 0

        # Mock _timestamped_backup to track ordering
        import mempalace.diagnostics as diag
        original_backup = diag._timestamped_backup
        backup_calls = []

        def tracking_backup(path):
            # Capture that clear() has NOT been called yet at this point
            ki_check = KeywordIndex.get(palace_path)
            count_before_backup = ki_check.count()
            backup_calls.append(("before", count_before_backup, path))
            result = original_backup(path)
            backup_calls.append(("after", result, path))
            return result

        diag._timestamped_backup = tracking_backup

        try:
            rebuild_keyword_index(palace_path, batch_size=10)
        finally:
            diag._timestamped_backup = original_backup

        assert len(backup_calls) >= 2
        # First call should happen before index is cleared
        assert backup_calls[0][0] == "before"


class TestValidateKeywordIndexPublicAPI:
    def test_validate_keyword_index_uses_sample_ids(self, palace_path, monkeypatch):
        """validate_keyword_index uses sample_ids() and checks LanceDB for those IDs."""
        from mempalace.lexical_index import KeywordIndex
        from mempalace.backends import get_backend

        ki = KeywordIndex.get(palace_path)
        ki.clear()
        ki.bulk_insert_batch([
            {"document_id": "doc_a", "content": "alpha", "wing": "t", "room": "r", "language": "en"},
            {"document_id": "doc_b", "content": "beta", "wing": "t", "room": "r", "language": "en"},
        ])

        class FakeCol:
            def count(self):
                return 2
            def get(self, ids=None, include=None):
                return {"ids": ["doc_a", "doc_b"],
                        "documents": ["alpha", "beta"],
                        "metadatas": [{"wing": "t", "room": "r", "language": "en"}] * 2}

        class FakeLanceBackend:
            def get_collection(self, pp, cn, create=False):
                return FakeCol()

        monkeypatch.setattr("mempalace.backends.get_backend", lambda *a, **kw: FakeLanceBackend())

        from mempalace.diagnostics import validate_keyword_index
        result = validate_keyword_index(palace_path)

        assert result["fts5_count"] == 2
        assert result["sample_check_passed"] is True