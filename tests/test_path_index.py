"""
tests/test_path_index.py — PathIndex unit tests.

Tests PathIndex CRUD, search priority, project_path isolation,
tombstone behavior, and ChromaDB import absence.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from mempalace.path_index import PathIndex


@pytest.fixture
def idx(tmp_path):
    """Fresh PathIndex per test, isolated in temp directory."""
    PathIndex._reset_for_testing()
    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)
    idx = PathIndex.get(palace_path)
    yield idx
    PathIndex._reset_for_testing()
    # Clean up
    db_path = os.path.join(palace_path, "path_index.sqlite3")
    if os.path.exists(db_path):
        os.remove(db_path)


class TestPathIndexSchema:
    def test_table_created(self, idx, tmp_path):
        """path_index table and indexes exist after init."""
        db_path = str(tmp_path / "palace" / "path_index.sqlite3")
        import sqlite3

        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='path_index'"
        )
        assert cur.fetchone() is not None, "path_index table not found"
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_path_source_file'"
        )
        assert cur.fetchone() is not None, "idx_path_source_file not found"
        conn.close()


class TestPathIndexWrite:
    def test_upsert_rows_single(self, idx):
        """Single row upsert creates record."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "repo_rel_path": "src/foo.py",
                    "language": "Python",
                    "chunk_kind": "function",
                    "symbol_name": "foo",
                    "line_start": 10,
                    "line_end": 20,
                    "wing": "repo",
                    "room": "src_main_py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"
        assert results[0]["source_file"] == "/repo/src/foo.py"
        assert results[0]["language"] == "Python"

    def test_upsert_rows_multiple(self, idx):
        """Multiple row upsert creates all records."""
        rows = [
            {
                "document_id": f"doc{i}",
                "source_file": f"/repo/src/file{i}.py",
                "language": "Python",
                "is_latest": True,
            }
            for i in range(5)
        ]
        idx.upsert_rows(rows)
        results = idx.search_path("/repo/src/file2.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc2"

    def test_upsert_rows_overwrites(self, idx):
        """Upsert with same document_id updates existing row."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "language": "Python",
                    "is_latest": True,
                }
            ]
        )
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "language": "Python",
                    "symbol_name": "bar",  # Update
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 1
        assert results[0]["symbol_name"] == "bar"
        assert idx.count() == 1

    def test_delete_rows(self, idx):
        """Delete removes rows from path index."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                },
                {
                    "document_id": "doc2",
                    "source_file": "/repo/src/bar.py",
                    "is_latest": True,
                },
            ]
        )
        idx.delete_rows(["doc1"])
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 0
        results2 = idx.search_path("/repo/src/bar.py")
        assert len(results2) == 1
        assert results2[0]["document_id"] == "doc2"

    def test_delete_rows_batch(self, idx):
        """Batch delete removes multiple rows."""
        idx.upsert_rows(
            [
                {
                    "document_id": f"doc{i}",
                    "source_file": f"/repo/src/file{i}.py",
                    "is_latest": True,
                }
                for i in range(10)
            ]
        )
        idx.delete_rows([f"doc{i}" for i in range(5)])
        assert idx.count() == 5

    def test_mark_tombstoned(self, idx):
        """mark_tombstoned sets is_latest=0 but does not delete."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        idx.mark_tombstoned(["doc1"])
        # Row still exists but filtered out by is_latest=1
        import sqlite3

        conn = sqlite3.connect(idx.db_path)
        cur = conn.execute(
            "SELECT is_latest FROM path_index WHERE document_id='doc1'"
        ).fetchone()
        conn.close()
        assert cur is not None
        assert cur[0] == 0
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 0


class TestPathIndexSearch:
    def test_exact_source_file_match(self, idx):
        """Exact source_file match returns row."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_relative_path_match(self, idx):
        """Exact repo_rel_path match returns row."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "repo_rel_path": "src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("src/foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_suffix_match(self, idx):
        """source_file.endswith(query) returns row."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/deep/path/foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_suffix_match_with_slash(self, idx):
        """Trailing slash variant of suffix match."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("/foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_basename_match(self, idx):
        """Basename-only match returns row."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_basename_not_suffix(self, idx):
        """When query contains /, basename match should NOT fire (only suffix match)."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        # query="src/bar.py" — does NOT match suffix (foo.py != bar.py),
        # and contains / so basename match is skipped → 0 results
        results = idx.search_path("src/bar.py")
        assert len(results) == 0

    def test_no_chroma_import(self, idx):
        """Verify no chromadb module is imported by path_index."""
        import sys

        mods = [m for m in sys.modules if "chroma" in m.lower()]
        assert len(mods) == 0, f"chroma imported: {mods}"


class TestPathIndexProjectIsolation:
    def test_project_path_strict_filter(self, idx):
        """project_path filter is strict — source_file must start with project_path."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                },
                {
                    "document_id": "doc2",
                    "source_file": "/other/src/bar.py",
                    "is_latest": True,
                },
            ]
        )
        results = idx.search_path("foo.py", project_path="/repo")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

        results2 = idx.search_path("bar.py", project_path="/repo")
        assert len(results2) == 0

    def test_project_path_prefix_stripping(self, idx):
        """Rows outside project_path are excluded even if name matches."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/file.py",
                    "is_latest": True,
                },
                {
                    "document_id": "doc2",
                    "source_file": "/other/file.py",
                    "is_latest": True,
                },
            ]
        )
        results = idx.search_path("file.py", project_path="/repo")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_language_filter(self, idx):
        """Language filter restricts results."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "language": "Python",
                    "is_latest": True,
                },
                {
                    "document_id": "doc2",
                    "source_file": "/repo/src/bar.go",
                    "language": "Go",
                    "is_latest": True,
                },
            ]
        )
        results = idx.search_path("foo.py", language="Python")
        assert len(results) == 1
        results2 = idx.search_path("bar.go", language="Python")
        assert len(results2) == 0

    def test_limit(self, idx):
        """search_path respects limit parameter."""
        for i in range(20):
            idx.upsert_rows(
                [
                    {
                        "document_id": f"doc{i}",
                        "source_file": f"/repo/src/file{i}.py",
                        "is_latest": True,
                    }
                ]
            )
        results = idx.search_path("file", limit=5)
        assert len(results) <= 5


class TestPathIndexTombstone:
    def test_tombstoned_not_returned(self, idx):
        """Rows with is_latest=0 are not returned."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        idx.mark_tombstoned(["doc1"])
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 0

    def test_tombstoned_count(self, idx):
        """count() excludes tombstoned rows."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        assert idx.count() == 1
        idx.mark_tombstoned(["doc1"])
        assert idx.count() == 0


class TestPathIndexBasename:
    def test_basename_extracted(self, idx):
        """basename is auto-extracted from source_file if not provided."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/deep/path/foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("foo.py")
        assert len(results) == 1
        assert results[0]["source_file"] == "/repo/deep/path/foo.py"

    def test_basename_provided(self, idx):
        """basename provided explicitly is used."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "basename": "foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("foo.py")
        assert len(results) == 1


class TestPathIndexRepoRelPath:
    def test_repo_rel_path_compute(self):
        """compute_repo_rel_path strips common prefix."""
        result = PathIndex.compute_repo_rel_path(
            "/repo/src/foo.py", "/repo"
        )
        assert result == "src/foo.py"

    def test_repo_rel_path_no_match(self):
        """compute_repo_rel_path returns source_file when no prefix match."""
        result = PathIndex.compute_repo_rel_path(
            "/other/src/foo.py", "/repo"
        )
        assert result == "/other/src/foo.py"

    def test_repo_rel_path_empty(self):
        """compute_repo_rel_path returns source_file when input empty."""
        result = PathIndex.compute_repo_rel_path("", "/repo")
        assert result == ""

    def test_repo_rel_path_in_search(self, idx):
        """Rows with matching repo_rel_path are found."""
        idx.upsert_rows(
            [
                {
                    "document_id": "doc1",
                    "source_file": "/repo/src/foo.py",
                    "repo_rel_path": "src/foo.py",
                    "is_latest": True,
                }
            ]
        )
        results = idx.search_path("src/foo.py")
        assert len(results) == 1
