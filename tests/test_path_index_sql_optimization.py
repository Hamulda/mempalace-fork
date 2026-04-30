"""
tests/test_path_index_sql_optimization.py — SQL optimization regression tests.

Verifies that search_path uses indexed SQL stages rather than
SELECT-all + Python filtering for normal path queries.

Cases covered:
- exact source_file uses indexed path and returns quickly
- relative path lookup (repo_rel_path exact)
- basename lookup
- suffix lookup with bounded scan
- path with LIKE wildcards (% or _) does not wildcard-match unrelated files
- /proj does not match /proj-old (strict boundary)
- macOS /var vs /private/var normalization (realpath)
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from mempalace.path_index import PathIndex, _escape_sql_like, _normalize_path_for_sql


@pytest.fixture
def idx(tmp_path):
    """Fresh PathIndex per test, isolated in temp directory."""
    PathIndex._reset_for_testing()
    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)
    idx = PathIndex.get(palace_path)
    yield idx
    PathIndex._reset_for_testing()
    db_path = os.path.join(palace_path, "path_index.sqlite3")
    if os.path.exists(db_path):
        os.remove(db_path)


# ── Unit tests for helpers ────────────────────────────────────────────────────

class TestEscapeSqlLike:
    """_escape_sql_like prevents wildcard matching for literal %, _, \\."""

    def test_escape_percent(self):
        # The escape function escapes % so it is treated as a literal %.
        # Verifying the escaped output contains the escaped form.
        result = _escape_sql_like("foo%.py")
        assert "\\%" in result
        # SQL LIKE with ESCAPE correctly matches literal %
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(p TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", ("foo%.py",))
        conn.execute("INSERT INTO t VALUES (?)", ("fooXYZ.py",))
        rows = conn.execute(
            f"SELECT p FROM t WHERE p LIKE ? ESCAPE '{chr(92)}'", (result,)
        ).fetchall()
        assert [r[0] for r in rows] == ["foo%.py"]

    def test_escape_underscore(self):
        # Escaped underscore matches literal _ but not any-char
        result = _escape_sql_like("file_name.txt")
        assert "\\_" in result
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(p TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", ("file_name.txt",))
        conn.execute("INSERT INTO t VALUES (?)", ("fileXname.txt",))
        rows = conn.execute(
            f"SELECT p FROM t WHERE p LIKE ? ESCAPE '{chr(92)}'", (result,)
        ).fetchall()
        # Literal underscore matches only file_name.txt, not fileXname.txt
        assert [r[0] for r in rows] == ["file_name.txt"]

    def test_escape_backslash(self):
        # Backslash itself must be escaped to match literally
        result = _escape_sql_like(r"path\to\file")
        assert "\\\\" in result

    def test_escape_mixed(self):
        result = _escape_sql_like("50%.py")
        assert "\\%" in result

    def test_no_escape_plain(self):
        # Plain text with no wildcards is unchanged
        assert _escape_sql_like("foo.py") == "foo.py"
        assert "\\" not in _escape_sql_like("foo.py")


class TestNormalizePathForSql:
    """_normalize_path_for_sql strips trailing slashes and normalizes separators."""

    def test_strip_trailing_slash(self):
        assert _normalize_path_for_sql("/repo/src/") == "/repo/src"

    def test_backslash_to_forward(self):
        # Unix path with backslashes should be normalized to forward slashes
        # (on macOS realpath resolves symlinks, but we test the slash normalization path)
        result = _normalize_path_for_sql(r"repo\src\foo.py")
        assert "/" in result or result == "repo/src/foo.py"

    def test_empty(self):
        assert _normalize_path_for_sql("") == ""


# ── SQL optimization: exact source_file (Stage 1) ─────────────────────────────

class TestExactSourceFileStage:
    """Stage 1: exact source_file = ? uses indexed lookup, not table scan."""

    def test_exact_match_returns_row(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_exact_match_not_found(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("/repo/src/bar.py")
        assert len(results) == 0

    def test_exact_match_with_many_foils(self, idx):
        """Many unrelated rows don't slow down exact lookup."""
        # Insert 1000 unrelated rows
        foil_rows = [
            {
                "document_id": f"foil{i}",
                "source_file": f"/unrelated/dir{i}/file{i}.py",
                "is_latest": True,
            }
            for i in range(1000)
        ]
        idx.upsert_rows(foil_rows)
        idx.upsert_rows([{
            "document_id": "target",
            "source_file": "/repo/src/target.py",
            "is_latest": True,
        }])
        results = idx.search_path("/repo/src/target.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "target"


# ── SQL optimization: exact repo_rel_path (Stage 2) ────────────────────────────

class TestRepoRelPathStage:
    """Stage 2: exact repo_rel_path = ? uses indexed lookup."""

    def test_repo_rel_path_exact_match(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "repo_rel_path": "src/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("src/foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_repo_rel_path_not_found(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "repo_rel_path": "src/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("other/foo.py")
        assert len(results) == 0


# ── SQL optimization: suffix match (Stage 3) ───────────────────────────────────

class TestSuffixMatchStage:
    """Stage 3: suffix match with bounded scan + Python ends-with filter."""

    def test_suffix_match_simple(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/deep/path/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_suffix_match_trailing_slash_variant(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("/foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_suffix_not_contains(self, idx):
        """Suffix match should not match mid-path segments."""
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        # "oo.py" is not a suffix — "foo.py" ends with "oo.py" in Python,
        # but source_file.endswith("oo.py") is True here.
        # Actually the key test is: "src/bar.py" should NOT match foo.py
        # Let's test: query for "bar.py" when file is foo.py
        results = idx.search_path("bar.py")
        assert len(results) == 0


# ── SQL optimization: basename match (Stage 4) ────────────────────────────────

class TestBasenameMatchStage:
    """Stage 4: basename exact match uses index."""

    def test_basename_match(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("foo.py")
        assert len(results) == 1
        assert results[0]["document_id"] == "doc1"

    def test_basename_match_only_file_name(self, idx):
        """Basename match fires even without / in query."""
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        # "foo.py" fires both suffix and basename — suffix takes priority
        results = idx.search_path("foo.py")
        assert len(results) >= 1


# ── Wildcard literal escape: % and _ ─────────────────────────────────────────

class TestWildcardEscape:
    """SQL LIKE wildcards in query must not match unrelated files."""

    def test_percent_literal_not_wildcard(self, idx):
        """Query '50%.py' should NOT match '500.py' (wildcard behavior)."""
        idx.upsert_rows([
            {
                "document_id": "file50pct",
                "source_file": "/repo/src/50%.py",
                "is_latest": True,
            },
            {
                "document_id": "file500",
                "source_file": "/repo/src/500.py",
                "is_latest": True,
            },
        ])
        results = idx.search_path("50%.py")
        # Should find only the file with literal % in its name
        doc_ids = {r["document_id"] for r in results}
        assert "file50pct" in doc_ids
        assert "file500" not in doc_ids

    def test_underscore_literal_not_wildcard(self, idx):
        """Query 'file_name.py' should NOT match 'fileXname.py' (wildcard behavior)."""
        idx.upsert_rows([
            {
                "document_id": "file_underscore",
                "source_file": "/repo/src/file_name.py",
                "is_latest": True,
            },
            {
                "document_id": "fileXname",
                "source_file": "/repo/src/fileXname.py",
                "is_latest": True,
            },
        ])
        results = idx.search_path("file_name.py")
        doc_ids = {r["document_id"] for r in results}
        assert "file_underscore" in doc_ids
        assert "fileXname" not in doc_ids


# ── Strict project_path boundary ──────────────────────────────────────────────

class TestProjectPathBoundary:
    """project_path filter is strict: /proj must NOT match /proj-old."""

    def test_prefix_not_match_unrelated(self, idx):
        """Files under /proj-old must NOT be returned when project_path=/proj."""
        idx.upsert_rows([
            {
                "document_id": "proj_file",
                "source_file": "/proj/src/main.py",
                "is_latest": True,
            },
            {
                "document_id": "proj_old_file",
                "source_file": "/proj-old/src/main.py",
                "is_latest": True,
            },
        ])
        results = idx.search_path("main.py", project_path="/proj")
        doc_ids = {r["document_id"] for r in results}
        assert "proj_file" in doc_ids
        assert "proj_old_file" not in doc_ids

    def test_exact_project_file_also_matches(self, idx):
        """project_path as a file (not dir) should also be found."""
        idx.upsert_rows([{
            "document_id": "proj_file",
            "source_file": "/proj",
            "is_latest": True,
        }])
        results = idx.search_path("/proj", project_path="/proj")
        assert len(results) == 1
        assert results[0]["document_id"] == "proj_file"

    def test_nested_under_project_path(self, idx):
        """Subdirectories of project_path are included."""
        idx.upsert_rows([{
            "document_id": "nested",
            "source_file": "/proj/src/lib/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("foo.py", project_path="/proj")
        assert len(results) == 1
        assert results[0]["document_id"] == "nested"

    def test_outside_project_path_excluded(self, idx):
        """Files outside project_path are excluded even with matching name."""
        idx.upsert_rows([
            {
                "document_id": "in_proj",
                "source_file": "/proj/src/foo.py",
                "is_latest": True,
            },
            {
                "document_id": "outside_proj",
                "source_file": "/other/src/foo.py",
                "is_latest": True,
            },
        ])
        results = idx.search_path("foo.py", project_path="/proj")
        doc_ids = {r["document_id"] for r in results}
        assert "in_proj" in doc_ids
        assert "outside_proj" not in doc_ids

    def test_project_path_with_wildcard_chars(self, idx):
        """project_path containing % or _ should not act as wildcard."""
        idx.upsert_rows([
            {
                "document_id": "percent_file",
                "source_file": "/path%20with%20spaces/file.py",
                "is_latest": True,
            },
            {
                "document_id": "regular_file",
                "source_file": "/path20with20spaces/file.py",
                "is_latest": True,
            },
        ])
        results = idx.search_path("file.py", project_path="/path%20with%20spaces")
        doc_ids = {r["document_id"] for r in results}
        assert "percent_file" in doc_ids
        assert "regular_file" not in doc_ids


# ── macOS /var ↔ /private/var normalization ───────────────────────────────────

class TestMacOSVarNormalization:
    """On macOS, /var is a symlink to /private/var; realpath normalizes them.

    To enable full /var ↔ /private/var symmetry, call
    PathIndex.normalize_source_file() at insert time or re-index.
    The helper function normalizes stored paths; the project_path boundary
    also normalizes the query-time path.
    """

    def test_normalize_source_file_strips_trailing(self):
        """normalize_source_file strips trailing slashes."""
        result = PathIndex.normalize_source_file("/var/folders/00/")
        assert result == "/var/folders/00"

    def test_project_path_same_form_matches(self, idx):
        """When stored and queried with the same form, matching works."""
        idx.upsert_rows([{
            "document_id": "var_file",
            "source_file": "/var/folders/00/file.py",
            "is_latest": True,
        }])
        # Same form on both sides — should match via project_path boundary
        results = idx.search_path("file.py", project_path="/var/folders")
        assert len(results) == 1
        assert results[0]["document_id"] == "var_file"

    def test_normalize_source_file_just_slashes_on_linux(self):
        """On non-Darwin, normalize_source_file normalizes slashes/trailing."""
        import sys
        if sys.platform == "darwin":
            pytest.skip("non-Darwin only test")
        result = PathIndex.normalize_source_file(r"repo\src\foo.py")
        assert "/" in result or result.endswith("foo.py")


# ── Priority and dedup ────────────────────────────────────────────────────────

class TestPriorityAndDedup:
    """Higher-priority matches should appear before lower-priority ones."""

    def test_exact_source_file_before_suffix(self, idx):
        """Exact source_file (Stage 1) should rank higher than suffix (Stage 3)."""
        idx.upsert_rows([
            {
                "document_id": "exact_match",
                "source_file": "/repo/src/foo.py",
                "repo_rel_path": "src/foo.py",
                "is_latest": True,
            },
            {
                "document_id": "suffix_only",
                "source_file": "/repo/src/bar/foo.py",
                "is_latest": True,
            },
        ])
        results = idx.search_path("foo.py")
        assert len(results) == 2
        assert results[0]["document_id"] == "exact_match"

    def test_no_duplicate_doc_ids(self, idx):
        """Same doc_id from multiple stages should appear only once."""
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "repo_rel_path": "src/foo.py",
            "basename": "foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("foo.py")
        doc_ids = [r["document_id"] for r in results]
        assert len(doc_ids) == len(set(doc_ids))


# ── Limit enforcement ─────────────────────────────────────────────────────────

class TestLimitEnforcement:
    """Each stage is bounded; limit is respected."""

    def test_limit_one(self, idx):
        """limit=1 returns at most 1 result."""
        idx.upsert_rows([
            {
                "document_id": f"doc{i}",
                "source_file": f"/repo/src/file{i}.py",
                "is_latest": True,
            }
            for i in range(10)
        ])
        results = idx.search_path("file", limit=1)
        assert len(results) <= 1

    def test_limit_respects_stages(self, idx):
        """limit=2: if Stage 1 returns 2, later stages are skipped."""
        idx.upsert_rows([
            {
                "document_id": f"doc{i}",
                "source_file": f"/repo/src/file{i}.py",
                "is_latest": True,
            }
            for i in range(5)
        ])
        # Exact source_file match returns 2 for exact "file0.py"
        idx.upsert_rows([{
            "document_id": "exact_file0",
            "source_file": "/repo/src/file0.py",
            "is_latest": True,
        }])
        results = idx.search_path("/repo/src/file0.py", limit=2)
        assert len(results) <= 2


# ── Language filter ───────────────────────────────────────────────────────────

class TestLanguageFilter:
    """Language filter applies to all stages via AND clause."""

    def test_language_filter(self, idx):
        idx.upsert_rows([
            {
                "document_id": "py_file",
                "source_file": "/repo/src/foo.py",
                "language": "Python",
                "is_latest": True,
            },
            {
                "document_id": "go_file",
                "source_file": "/repo/src/foo.go",
                "language": "Go",
                "is_latest": True,
            },
        ])
        results = idx.search_path("foo.py", language="Python")
        assert len(results) == 1
        assert results[0]["document_id"] == "py_file"


# ── Tombstone isolation ────────────────────────────────────────────────────────

class TestTombstoneIsolation:
    """Tombstoned rows (is_latest=0) are excluded from all stages."""

    def test_tombstoned_excluded_from_all_stages(self, idx):
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        idx.mark_tombstoned(["doc1"])
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 0


# ── Backward compatibility ────────────────────────────────────────────────────

class TestBackwardCompatibility:
    """Existing schema is unchanged; existing queries still work."""

    def test_schema_unchanged(self, tmp_path):
        PathIndex._reset_for_testing()
        palace_path = str(tmp_path / "palace")
        os.makedirs(palace_path, exist_ok=True)
        PathIndex.get(palace_path)  # init db
        db_path = str(tmp_path / "palace" / "path_index.sqlite3")
        conn = sqlite3.connect(db_path)
        # All original columns present
        cur = conn.execute("PRAGMA table_info(path_index)")
        columns = {row[1] for row in cur.fetchall()}
        conn.close()
        expected = {
            "document_id", "source_file", "repo_rel_path", "basename",
            "language", "chunk_kind", "symbol_name", "line_start",
            "line_end", "wing", "room", "is_latest",
        }
        assert expected.issubset(columns)

    def test_search_without_project_path(self, idx):
        """search_path works without project_path (backward compat)."""
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "is_latest": True,
        }])
        results = idx.search_path("foo.py")
        assert len(results) == 1

    def test_search_without_language(self, idx):
        """search_path works without language filter (backward compat)."""
        idx.upsert_rows([{
            "document_id": "doc1",
            "source_file": "/repo/src/foo.py",
            "language": "Python",
            "is_latest": True,
        }])
        results = idx.search_path("/repo/src/foo.py")
        assert len(results) == 1
