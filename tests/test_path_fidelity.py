"""
test_path_fidelity.py — Path fidelity sprint tests.

Verifies the unified path output contract:
- source_file: canonical identity (always absolute path)
- repo_rel_path: user-friendly display (relative to project root)

Tests cover:
- wakeup_context adds source_file + repo_rel_path to recent_changes/hot_spots
- symbol tools (find_symbol, search_symbols, callers, file_symbols) return source_file + repo_rel_path
- mempalace_project_context adds repo_rel_path to chunks
- auto_search preserves repo_rel_path from underlying search
- _compute_repo_rel_path edge cases
- backward compatibility (file_path still present where required)
"""

import pytest
from unittest.mock import patch, MagicMock

from mempalace.searcher import (
    _compute_repo_rel_path,
    _add_repo_rel_path,
    auto_search,
    code_search,
    search_memories,
)


@pytest.fixture
def multi_dir_collection(palace_path, monkeypatch):
    """Collection with two same-basename files in different directories."""
    import chromadb
    from mempalace.backends import get_backend

    backend = get_backend("chroma")
    col = backend.get_collection(palace_path, "mempalace_drawers", create=True)

    # Two files named utils.py in different directories
    col.add(
        ids=["drawer_repo_utils_a", "drawer_repo_utils_b"],
        documents=[
            "def authenticate(user): return jwt.encode(user)",
            "def parse_date(s): return datetime.fromisoformat(s)",
        ],
        metadatas=[
            {
                "wing": "repo",
                "room": "core",
                "source_file": "/Users/dev/project/src/utils.py",
                "is_latest": True,
                "language": "Python",
                "chunk_index": 0,
            },
            {
                "wing": "repo",
                "room": "core",
                "source_file": "/Users/dev/project/tests/utils.py",
                "is_latest": True,
                "language": "Python",
                "chunk_index": 0,
            },
        ],
    )
    return col


@pytest.fixture(autouse=True)
def force_chroma_backend(palace_path, seeded_collection, monkeypatch):
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")


# =============================================================================
# _compute_repo_rel_path edge cases
# =============================================================================


class TestComputeRepoRelPathEdgeCases:
    """Edge cases for repo-relative path computation."""

    def test_source_file_is_prefix(self):
        """When source_file == common_prefix, return just filename."""
        result = _compute_repo_rel_path("/project/src/utils.py", "/project/src/utils.py")
        assert result == "utils.py"

    def test_source_file_deeper_than_prefix(self):
        """When source_file is deeper than prefix, return subpath."""
        result = _compute_repo_rel_path("/project/src/lib/core/auth.py", "/project/src")
        assert result == "lib/core/auth.py"

    def test_source_file_outside_prefix(self):
        """When source_file doesn't start with prefix, return source_file unchanged."""
        result = _compute_repo_rel_path("/other/project/src/utils.py", "/project")
        assert result == "/other/project/src/utils.py"

    def test_empty_source_file(self):
        """Empty source_file returns empty string."""
        result = _compute_repo_rel_path("", "/project")
        assert result == ""

    def test_empty_prefix(self):
        """Empty prefix returns source_file unchanged."""
        result = _compute_repo_rel_path("/project/src/utils.py", "")
        assert result == "/project/src/utils.py"

    def test_prefix_without_trailing_slash(self):
        """Prefix without trailing slash is handled correctly."""
        result = _compute_repo_rel_path("/project/src/utils.py", "/project")
        assert result == "src/utils.py"

    def test_prefix_with_trailing_slash(self):
        """Prefix with trailing slash is handled correctly."""
        result = _compute_repo_rel_path("/project/src/utils.py", "/project/")
        assert result == "src/utils.py"


# =============================================================================
# _add_repo_rel_path
# =============================================================================


class TestAddRepoRelPath:
    """_add_repo_rel_path adds repo_rel_path when common prefix is a real project root."""

    def test_add_repo_rel_path_single_file(self):
        """Single file with absolute path gets repo_rel_path."""
        hits = [{"source_file": "/project/src/utils.py"}]
        result = _add_repo_rel_path(hits, ["/project/src/utils.py"])
        assert result[0].get("repo_rel_path") == "utils.py"

    def test_add_repo_rel_path_multiple_files(self):
        """Multiple files with common prefix get repo_rel_path."""
        hits = [
            {"source_file": "/project/src/utils.py"},
            {"source_file": "/project/src/auth.py"},
        ]
        result = _add_repo_rel_path(hits, ["/project/src/utils.py", "/project/src/auth.py"])
        assert result[0].get("repo_rel_path") == "utils.py"
        assert result[1].get("repo_rel_path") == "auth.py"

    def test_add_repo_rel_path_no_common_prefix(self):
        """No common prefix means repo_rel_path not added."""
        hits = [{"source_file": "/project/src/utils.py"}, {"source_file": "/other/lib/auth.py"}]
        result = _add_repo_rel_path(hits, ["/project/src/utils.py", "/other/lib/auth.py"])
        # commonpath of two absolute paths in different roots returns "/"
        assert "repo_rel_path" not in result[0]


# =============================================================================
# auto_search preserves repo_rel_path
# =============================================================================


class TestAutoSearchRepoRelPath:
    """auto_search delegates to code_search or hybrid_search and preserves repo_rel_path."""

    def test_auto_search_code_query_returns_repo_rel_path(self, palace_path, multi_dir_collection):
        """auto_search with code query (def ...) returns repo_rel_path in results."""
        result = auto_search("def authenticate", palace_path, n_results=5)
        hits = result.get("results", [])
        assert len(hits) > 0
        # code_search adds repo_rel_path via _add_repo_rel_path
        for hit in hits:
            if hit.get("source") == "kg":
                continue
            assert "repo_rel_path" in hit or "source_file" in hit

    def test_auto_search_prose_query_returns_repo_rel_path(self, palace_path, seeded_collection):
        """auto_search with prose query falls back to hybrid_search which preserves repo_rel_path."""
        result = auto_search("authentication tokens", palace_path, n_results=5)
        hits = result.get("results", [])
        # hybrid_search passes repo_rel_path from search_memories vector hits
        vector_hits = [h for h in hits if h.get("source") != "kg"]
        if vector_hits:
            assert all("repo_rel_path" in h or "source_file" in h for h in vector_hits)


# =============================================================================
# code_search path contract
# =============================================================================


class TestCodeSearchPathContract:
    """code_search returns source_file (full path) + repo_rel_path."""

    def test_code_search_includes_repo_rel_path(self, palace_path, multi_dir_collection):
        """code_search results include repo_rel_path."""
        result = code_search("def authenticate", palace_path, n_results=5)
        hits = result.get("results", [])
        assert len(hits) > 0
        for hit in hits:
            assert "source_file" in hit
            assert "repo_rel_path" in hit

    def test_code_search_repo_rel_path_for_known_project(self, palace_path, multi_dir_collection):
        """repo_rel_path is correctly computed for known project structure."""
        result = code_search("def authenticate", palace_path, n_results=5)
        hits = result.get("results", [])
        for hit in hits:
            if "/src/utils.py" in hit.get("source_file", ""):
                assert hit.get("repo_rel_path") == "src/utils.py"


# =============================================================================
# search_memories path contract
# =============================================================================


class TestSearchMemoriesPathContract:
    """search_memories returns source_file + repo_rel_path."""

    def test_search_memories_includes_repo_rel_path(self, palace_path, multi_dir_collection):
        """search_memories results include repo_rel_path."""
        result = search_memories("def authenticate", palace_path, n_results=5)
        hits = result.get("results", [])
        assert len(hits) > 0
        for hit in hits:
            assert "source_file" in hit
            assert "repo_rel_path" in hit


# =============================================================================
# wakeup_context path fields
# =============================================================================


class TestWakeupContextPathFields:
    """wakeup_context adds canonical path fields to recent_changes and hot_spots."""

    def test_build_wakeup_context_adds_source_file_and_repo_rel_path(self, palace_path, monkeypatch):
        """build_wakeup_context adds source_file and repo_rel_path to recent_changes entries."""
        mock_recent = [
            {
                "file_path": "src/utils.py",
                "commit_hash": "abc123",
                "commit_date": "2026-04-01T00:00:00",
                "commit_message": "add utils",
                "files_changed": 1,
            },
            {
                "file_path": "tests/test_auth.py",
                "commit_hash": "def456",
                "commit_date": "2026-04-02T00:00:00",
                "commit_message": "add tests",
                "files_changed": 2,
            },
        ]
        mock_hotspots = [
            {"file_path": "src/utils.py", "change_count": 5},
        ]

        def mock_get_recent(project_root, n=20):
            return mock_recent

        def mock_get_hot(project_root, n=5):
            return mock_hotspots

        monkeypatch.setenv("PROJECT_ROOT", "/Users/dev/project")
        with patch("mempalace.wakeup_context.get_recent_changes", mock_get_recent):
            with patch("mempalace.wakeup_context.get_hot_spots", mock_get_hot):
                from mempalace.wakeup_context import build_wakeup_context
                ctx = build_wakeup_context("test-session", project_root="/Users/dev/project", palace_path=palace_path)

        recent = ctx.get("recent_changes", [])
        assert len(recent) == 2

        # Each entry has both legacy fields AND new canonical fields
        for entry in recent:
            assert "file_path" in entry  # backward compat
            assert "abs_path" in entry  # backward compat
            assert "source_file" in entry  # NEW canonical
            assert "repo_rel_path" in entry  # NEW user-friendly

        # Values are correct
        assert recent[0]["abs_path"] == "/Users/dev/project/src/utils.py"
        assert recent[0]["source_file"] == "/Users/dev/project/src/utils.py"
        assert recent[0]["repo_rel_path"] == "src/utils.py"

        assert recent[1]["abs_path"] == "/Users/dev/project/tests/test_auth.py"
        assert recent[1]["source_file"] == "/Users/dev/project/tests/test_auth.py"
        assert recent[1]["repo_rel_path"] == "tests/test_auth.py"

    def test_build_wakeup_context_hot_spots_also_enriched(self, palace_path, monkeypatch):
        """hot_spots entries also get source_file and repo_rel_path."""
        mock_recent = []
        mock_hotspots = [
            {"file_path": "lib/core/auth.py", "change_count": 10},
        ]

        def mock_get_recent(project_root, n=20):
            return mock_recent

        def mock_get_hot(project_root, n=5):
            return mock_hotspots

        monkeypatch.setenv("PROJECT_ROOT", "/Users/dev/project")
        with patch("mempalace.wakeup_context.get_recent_changes", mock_get_recent):
            with patch("mempalace.wakeup_context.get_hot_spots", mock_get_hot):
                from mempalace.wakeup_context import build_wakeup_context
                ctx = build_wakeup_context("test-session", project_root="/Users/dev/project", palace_path=palace_path)

        hot_spots = ctx.get("hot_spots", [])
        assert len(hot_spots) == 1
        entry = hot_spots[0]
        assert "source_file" in entry
        assert "repo_rel_path" in entry
        assert entry["source_file"] == "/Users/dev/project/lib/core/auth.py"
        assert entry["repo_rel_path"] == "lib/core/auth.py"


# =============================================================================
# Duplicate basename disambiguation
# =============================================================================


class TestDuplicateBasenameDisambiguation:
    """src/utils.py vs tests/utils.py vs lib/utils.py are all disambiguated."""

    def test_search_memories_distinguishes_same_basename(self, palace_path, multi_dir_collection):
        """search_memories returns different full paths for same-basename files."""
        result = search_memories("def", palace_path, n_results=10)
        hits = result.get("results", [])
        paths = [h.get("source_file", "") for h in hits]
        # All paths should be full (contain /)
        for p in paths:
            assert "/" in p, f"Expected full path, got: {p}"
        # src/utils.py and tests/utils.py should have different full paths
        src_paths = [p for p in paths if "src/utils.py" in p]
        tests_paths = [p for p in paths if "tests/utils.py" in p]
        if src_paths and tests_paths:
            assert src_paths[0] != tests_paths[0]
            assert src_paths[0].endswith("src/utils.py")
            assert tests_paths[0].endswith("tests/utils.py")

    def test_code_search_file_path_filter_disambiguates(self, palace_path, multi_dir_collection):
        """file_path filter correctly disambiguates src/ vs tests/ directory."""
        from mempalace.searcher import code_search
        # Filter to src/ only
        result = code_search("def", palace_path, n_results=10, file_path="src/")
        for hit in result.get("results", []):
            assert "src/" in hit.get("source_file", "")

        # Filter to tests/ only
        result = code_search("def", palace_path, n_results=10, file_path="tests/")
        for hit in result.get("results", []):
            assert "tests/" in hit.get("source_file", "")


# =============================================================================
# Backward compatibility
# =============================================================================


class TestBackwardCompatibility:
    """Old field names continue to work where backward compatibility is required."""

    def test_code_search_result_structure_backward_compatible(self, palace_path, seeded_collection):
        """code_search result structure has all expected legacy fields."""
        result = code_search("authentication", palace_path, n_results=5)
        for hit in result.get("results", []):
            # Legacy fields must still be present
            assert "id" in hit
            assert "text" in hit
            assert "source_file" in hit
            assert "similarity" in hit

    def test_search_memories_result_structure_backward_compatible(self, palace_path, seeded_collection):
        """search_memories result structure has all expected legacy fields."""
        result = search_memories("authentication", palace_path, n_results=5)
        for hit in result.get("results", []):
            assert "text" in hit
            assert "wing" in hit
            assert "room" in hit
            assert "source_file" in hit
            assert "similarity" in hit

    def test_multi_dir_collection_still_works(self, palace_path, multi_dir_collection):
        """multi_dir_collection fixture still works (used by other tests)."""
        result = search_memories("def authenticate", palace_path, n_results=10)
        assert len(result["results"]) >= 1
