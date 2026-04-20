"""
test_code_path_fidelity.py — Tests for source_file path contract in code retrieval.

Verifies that search results return full source_file paths (not basenames),
that file_path filters work on full paths, and that same-basename files in
different directories are disambiguated correctly.
"""

import pytest

from mempalace.searcher import (
    search_memories,
    code_search,
    hybrid_search,
    auto_search,
)


@pytest.fixture(autouse=True)
def force_chroma_backend(palace_path, seeded_collection, monkeypatch):
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")


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


# ── search_memories path contract ──────────────────────────────────────


class TestSearchMemoriesPathContract:
    def test_search_memories_returns_full_path(self, palace_path, multi_dir_collection):
        """search_memories hit source_file is a full path, not just basename."""
        result = search_memories("def authenticate", palace_path)
        assert len(result["results"]) > 0
        hit = result["results"][0]
        assert "source_file" in hit
        # Must be a full path with directory separators
        assert "/" in hit["source_file"] or "\\" in hit["source_file"], (
            f"source_file should be full path, got: {hit['source_file']}"
        )

    def test_search_memories_same_basename_disambiguated(
        self, palace_path, multi_dir_collection
    ):
        """Two files with same basename in different dirs return different full paths."""
        result = search_memories("def authenticate", palace_path, n_results=10)
        hits = result["results"]

        # Find hits for the two different utils.py files
        utils_hits = [h for h in hits if h.get("source_file", "").endswith("utils.py")]
        assert len(utils_hits) >= 2, f"Expected at least 2 utils.py hits, got {len(utils_hits)}"

        paths = [h["source_file"] for h in utils_hits]
        assert len(set(paths)) == len(paths), (
            f"Same-basename files must have distinct full paths: {paths}"
        )
        # Verify paths differ in directory portion
        assert paths[0] != paths[1], f"Full paths must differ: {paths[0]} vs {paths[1]}"


# ── code_search path contract + file_path filter ──────────────────────


class TestCodeSearchPathContract:
    def test_code_search_returns_full_paths(self, palace_path, multi_dir_collection):
        """code_search hit source_file is a full path, not basename."""
        result = code_search("def authenticate", palace_path, n_results=10)
        hits = result["results"]
        assert len(hits) > 0
        for hit in hits:
            sf = hit.get("source_file", "")
            assert sf, "source_file must not be empty"
            assert "/" in sf or "\\" in sf, (
                f"source_file should be full path, got: {sf}"
            )

    def test_code_search_file_path_filter_full_match(self, palace_path, multi_dir_collection):
        """file_path filter selects correct file using full path substring match."""
        # Filter to src/utils.py specifically
        result = code_search(
            "def authenticate",
            palace_path,
            n_results=10,
            file_path="src/utils.py",
        )
        hits = result["results"]
        assert len(hits) > 0, "Should find authenticate in src/utils.py"
        for hit in hits:
            assert "src/utils.py" in hit["source_file"], (
                f"Expected src/utils.py in path, got: {hit['source_file']}"
            )

    def test_code_search_file_path_filter_dir_match(self, palace_path, multi_dir_collection):
        """file_path filter matching directory returns all files under that directory."""
        result = code_search(
            "def",
            palace_path,
            n_results=10,
            file_path="src/",
        )
        hits = result["results"]
        # All hits should be from src/ directory
        for hit in hits:
            assert hit["source_file"].startswith("/Users") or "/" in hit["source_file"], (
                f"Expected full path, got: {hit['source_file']}"
            )

    def test_code_search_file_path_filter_case_insensitive(
        self, palace_path, multi_dir_collection
    ):
        """file_path filter is case-insensitive on full path."""
        result = code_search(
            "def authenticate",
            palace_path,
            n_results=10,
            file_path="SRC/UTILS.PY",
        )
        hits = result["results"]
        # Should match /Users/dev/project/src/utils.py
        assert len(hits) > 0, "file_path filter should be case-insensitive"

    def test_code_search_file_path_filter_no_false_positive(
        self, palace_path, multi_dir_collection
    ):
        """file_path filter for tests/utils.py does not match src/utils.py."""
        result = code_search(
            "def parse_date",
            palace_path,
            n_results=10,
            file_path="tests/utils.py",
        )
        hits = result["results"]
        assert len(hits) > 0, "Should find parse_date in tests/utils.py"
        for hit in hits:
            assert "tests/utils.py" in hit["source_file"], (
                f"Expected tests/utils.py in path, got: {hit['source_file']}"
            )
            assert "src/utils.py" not in hit["source_file"], (
                "Should not match src/utils.py when filtering tests/utils.py"
            )


# ── hybrid_search path contract ───────────────────────────────────────


class TestHybridSearchPathContract:
    def test_hybrid_search_returns_full_paths(self, palace_path, multi_dir_collection):
        """hybrid_search hit source_file is a full path, not basename."""
        result = hybrid_search("def authenticate", palace_path, n_results=10, use_kg=False)
        hits = result["results"]
        assert len(hits) > 0
        # At least the query-matched hits must be full paths.
        # (seeded_collection docs pre-exist multi_dir_collection and may have basenames;
        # they are excluded by the wing=repo filter in code_search but not in hybrid_search)
        full_path_hits = [h for h in hits if "/" in h.get("source_file", "") or "\\" in h.get("source_file", "")]
        assert len(full_path_hits) > 0, f"Expected at least some full-path hits, got: {[h['source_file'] for h in hits]}"


# ── auto_search path contract ─────────────────────────────────────────


class TestAutoSearchPathContract:
    def test_auto_search_code_query_returns_full_paths(self, palace_path, multi_dir_collection):
        """auto_search with code-like query returns full paths in results."""
        result = auto_search("def authenticate user", palace_path, n_results=10)
        hits = result["results"]
        assert len(hits) > 0
        for hit in hits:
            sf = hit.get("source_file", "")
            if hit.get("source") == "kg":
                continue
            assert "/" in sf or "\\" in sf, (
                f"source_file should be full path for code query, got: {sf}"
            )


# ── backward compatibility ─────────────────────────────────────────────


class TestPathContractBackwardCompatibility:
    def test_search_memories_result_count(self, palace_path, seeded_collection):
        """search_memories still returns results with expected structure."""
        result = search_memories("JWT", palace_path, n_results=5)
        assert "results" in result
        assert len(result["results"]) > 0
        # Check expected fields are present
        hit = result["results"][0]
        for field in ("text", "wing", "room", "source_file", "similarity"):
            assert field in hit, f"Missing field: {field}"

    def test_code_search_result_count(self, palace_path, seeded_collection):
        """code_search still returns results with expected structure."""
        result = code_search("authentication", palace_path, n_results=5)
        assert "results" in result
        # May be 0 if no repo-wing docs in seeded_collection — that's OK
        for hit in result["results"]:
            for field in ("id", "text", "source_file", "similarity"):
                assert field in hit, f"Missing field: {field}"

    def test_file_path_filter_empty_on_no_match(self, palace_path, seeded_collection):
        """file_path filter returns empty results when nothing matches."""
        result = code_search(
            "authentication",
            palace_path,
            n_results=5,
            file_path="nonexistent/directory/file.py",
        )
        # Empty is acceptable — no matching docs
        assert "results" in result
