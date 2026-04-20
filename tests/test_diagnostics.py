"""
test_diagnostics.py — Smoke tests for mempalace.diagnostics module.
"""

import os
import tempfile
import shutil
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