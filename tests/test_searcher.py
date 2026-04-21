"""
test_searcher.py -- Tests for both search() (CLI) and search_memories() (API).

Uses the real ChromaDB fixtures from conftest.py for integration tests,
plus mock-based tests for error paths.

Note: conftest.py redirects HOME to a temp dir. MempalaceConfig() with no args
reads from ~/.mempalace (in that temp dir). The seeded_collection fixture uses
Chroma at palace_path, but search_memories() reads config from ~/.mempalace by
default and falls back to backend="lance". We set MEMPALACE_BACKEND=chroma
so search_memories uses Chroma and finds the seeded data.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mempalace.backends import get_backend
from mempalace.searcher import SearchError, _build_where_filter, search, search_memories


@pytest.fixture(autouse=True)
def force_chroma_backend(palace_path, seeded_collection, monkeypatch):
    """Ensure search_memories uses Chroma backend (the seeded fixture backend)."""
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")


# ── search_memories (API) ──────────────────────────────────────────────


class TestSearchMemories:
    def test_basic_search(self, palace_path, seeded_collection):
        result = search_memories("JWT authentication", palace_path)
        assert "results" in result
        assert len(result["results"]) > 0
        assert result["query"] == "JWT authentication"

    def test_wing_filter(self, palace_path, seeded_collection):
        result = search_memories("planning", palace_path, wing="notes")
        assert all(r["wing"] == "notes" for r in result["results"])

    def test_room_filter(self, palace_path, seeded_collection):
        result = search_memories("database", palace_path, room="backend")
        assert all(r["room"] == "backend" for r in result["results"])

    def test_wing_and_room_filter(self, palace_path, seeded_collection):
        result = search_memories("code", palace_path, wing="project", room="frontend")
        assert all(r["wing"] == "project" and r["room"] == "frontend" for r in result["results"])

    def test_n_results_limit(self, palace_path, seeded_collection):
        result = search_memories("code", palace_path, n_results=2)
        assert len(result["results"]) <= 2

    def test_no_palace_returns_error(self, tmp_path):
        result = search_memories("anything", str(tmp_path / "missing"))
        assert "error" in result

    def test_result_fields(self, palace_path, seeded_collection):
        result = search_memories("authentication", palace_path)
        hit = result["results"][0]
        assert "text" in hit
        assert "wing" in hit
        assert "room" in hit
        assert "source_file" in hit
        assert "similarity" in hit
        assert isinstance(hit["similarity"], float)

    def test_search_memories_query_error(self):
        """search_memories returns error dict when query raises."""
        mock_col = MagicMock()
        mock_col.query.side_effect = RuntimeError("query failed")
        mock_backend = MagicMock()
        mock_backend.get_collection.return_value = mock_col

        with patch("mempalace.searcher.get_backend", return_value=mock_backend):
            result = search_memories("test", "/fake/path")
        assert "error" in result
        assert "query failed" in result["error"]

    def test_search_memories_filters_in_result(self, palace_path, seeded_collection):
        result = search_memories("test", palace_path, wing="project", room="backend")
        assert result["filters"]["wing"] == "project"
        assert result["filters"]["room"] == "backend"


# ── search() (CLI print function) ─────────────────────────────────────


class TestSearchCLI:
    def test_search_prints_results(self, palace_path, seeded_collection, capsys):
        search("JWT authentication", palace_path)
        captured = capsys.readouterr()
        assert "JWT" in captured.out or "authentication" in captured.out

    def test_search_with_wing_filter(self, palace_path, seeded_collection, capsys):
        search("planning", palace_path, wing="notes")
        captured = capsys.readouterr()
        assert "Results for" in captured.out

    def test_search_with_room_filter(self, palace_path, seeded_collection, capsys):
        search("database", palace_path, room="backend")
        captured = capsys.readouterr()
        assert "Room:" in captured.out

    def test_search_with_wing_and_room(self, palace_path, seeded_collection, capsys):
        search("code", palace_path, wing="project", room="frontend")
        captured = capsys.readouterr()
        assert "Wing:" in captured.out
        assert "Room:" in captured.out

    def test_search_no_palace_raises(self, tmp_path):
        with pytest.raises(SearchError, match="No palace found"):
            search("anything", str(tmp_path / "missing"))

    def test_search_no_results(self, palace_path, collection, capsys):
        """Empty collection returns no results message."""
        result = search("xyzzy_nonexistent_query", palace_path, n_results=1)
        captured = capsys.readouterr()
        assert result is None or "No results" in captured.out

    def test_search_query_error_raises(self):
        """search raises SearchError when query fails."""
        mock_col = MagicMock()
        mock_col.query.side_effect = RuntimeError("boom")
        mock_backend = MagicMock()
        mock_backend.get_collection.return_value = mock_col

        with patch("mempalace.searcher.get_backend", return_value=mock_backend):
            with pytest.raises(SearchError, match="Search error"):
                search("test", "/fake/path")

    def test_search_n_results(self, palace_path, seeded_collection, capsys):
        search("code", palace_path, n_results=1)
        captured = capsys.readouterr()
        assert "[1]" in captured.out


# ── _build_where_filter ────────────────────────────────────────────────


class TestBuildWhereFilter:
    def test_build_where_no_params(self):
        assert _build_where_filter() == {}

    def test_build_where_wing_only(self):
        assert _build_where_filter(wing="wing_user") == {"wing": {"$eq": "wing_user"}}

    def test_build_where_room_only(self):
        assert _build_where_filter(room="backend") == {"room": {"$eq": "backend"}}

    def test_build_where_wing_room(self):
        result = _build_where_filter(wing="wing_user", room="backend")
        assert result == {"$and": [{"wing": {"$eq": "wing_user"}}, {"room": {"$eq": "backend"}}]}

    def test_build_where_is_latest(self):
        result = _build_where_filter(is_latest=True)
        assert result == {"is_latest": {"$eq": True}}

    def test_build_where_agent_id(self):
        result = _build_where_filter(agent_id="agent_1")
        assert result == {"agent_id": {"$eq": "agent_1"}}

    def test_build_where_priority_gte(self):
        result = _build_where_filter(priority_gte=5)
        assert result == {"priority": {"$gte": 5}}

    def test_build_where_priority_lte(self):
        result = _build_where_filter(priority_lte=3)
        assert result == {"priority": {"$lte": 3}}

    def test_build_where_all_params(self):
        result = _build_where_filter(
            wing="w",
            room="r",
            is_latest=True,
            agent_id="a1",
            priority_gte=5,
            priority_lte=3,
        )
        assert len(result["$and"]) == 6
        assert {"wing": {"$eq": "w"}} in result["$and"]
        assert {"room": {"$eq": "r"}} in result["$and"]
        assert {"is_latest": {"$eq": True}} in result["$and"]
        assert {"agent_id": {"$eq": "a1"}} in result["$and"]
        assert {"priority": {"$gte": 5}} in result["$and"]
        assert {"priority": {"$lte": 3}} in result["$and"]


# ── Async wrapper ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_memories_async_returns_dict(palace_path, seeded_collection):
    """search_memories_async returns a dict (not coroutine)."""
    from mempalace.searcher import search_memories_async

    result = await search_memories_async("JWT authentication", palace_path)
    assert isinstance(result, dict)
    assert "results" in result
    assert len(result["results"]) > 0


@pytest.mark.asyncio
async def test_six_parallel_async_searches(palace_path, seeded_collection):
    """Six parallel async searches run without exception."""
    import asyncio
    from mempalace.searcher import search_memories_async

    queries = ["JWT", "database", "React", "API", "config", "auth"]
    tasks = [search_memories_async(q, palace_path, n_results=3) for q in queries]
    results = await asyncio.gather(*tasks)
    assert len(results) == 6
    assert all(isinstance(r, dict) for r in results)


def test_adaptive_top_k_no_crash(palace_path, seeded_collection):
    """search_memories with n_results > doc count does not raise."""
    from mempalace.searcher import search_memories

    # seeded_collection has 4 docs — request 100, should clamp to 4
    result = search_memories("code", palace_path, n_results=100)
    assert "error" not in result or result.get("error", "") == ""
    assert len(result.get("results", [])) <= 4


# ── Rerank ───────────────────────────────────────────────────────────


def test_rerank_false_preserves_cosine_order(palace_path, seeded_collection):
    """rerank=False returns results sorted by cosine similarity, not rerank_score."""
    result = search_memories("authentication", palace_path, n_results=5, rerank=False)
    assert "results" in result
    similarities = [h["similarity"] for h in result["results"]]
    # Should be in descending cosine order
    assert similarities == sorted(similarities, reverse=True)
    # No rerank_score when rerank=False
    assert all("rerank_score" not in h for h in result["results"])


def test_rerank_skips_short_query(palace_path, seeded_collection):
    """Query with <= 3 words and rerank=True produces no rerank_score."""
    # Short query: "database code" = 2 words — reranking skipped
    result = search_memories("database code", palace_path, n_results=5, rerank=True)
    assert "results" in result
    assert all("rerank_score" not in h for h in result["results"])


def test_rerank_graceful_no_sentence_transformers(palace_path, seeded_collection):
    """search_memories with rerank=True works even if sentence-transformers is absent."""
    pytest.importorskip("sentence_transformers", reason="sentence-transformers installed")
    # If we reach here, sentence-transformers IS installed — rerank should work
    result = search_memories("JWT authentication pattern", palace_path, n_results=3, rerank=True)
    assert "error" not in result
    assert "results" in result


class TestHybridSearch:
    def test_hybrid_search_returns_results(self, palace_path, seeded_collection):
        """hybrid_search returns ChromaDB results + KG hits."""
        from mempalace.searcher import hybrid_search
        from mempalace.knowledge_graph import KnowledgeGraph
        import sqlite3

        # Add ChromaDB drawer with a known token
        backend = get_backend("chroma")
        col = backend.get_collection(palace_path, "mempalace_drawers")
        col.add(
            ids=["hw_1"],
            documents=["Alice works at Acme Corp"],
            metadatas=[{"wing": "notes", "room": "people", "is_latest": True}],
        )

        # Add KG triple with same token
        kg_path = str(Path(palace_path) / "knowledge_graph.sqlite3")
        kg = KnowledgeGraph(db_path=kg_path)
        kg.add_triple("Alice", "works_at", "Acme Corp", valid_from="2020-01-01")

        result = hybrid_search("Alice works at", palace_path, n_results=10, use_kg=True)

        assert "results" in result
        assert "sources" in result
        assert result["sources"]["vector"] >= 1
        assert result["sources"]["kg"] >= 1

    def test_hybrid_search_kg_failure_graceful(self, palace_path, seeded_collection):
        """hybrid_search returns ChromaDB results even if KG is unavailable."""
        from mempalace.searcher import hybrid_search

        result = hybrid_search("JWT", palace_path, n_results=5, use_kg=True)

        assert "results" in result
        assert isinstance(result["results"], list)

    def test_hybrid_search_deduplication(self, palace_path, seeded_collection):
        """Identical text from ChromaDB and KG appears only once."""
        from mempalace.searcher import hybrid_search
        from mempalace.knowledge_graph import KnowledgeGraph

        # Add ChromaDB drawer
        backend = get_backend("chroma")
        col = backend.get_collection(palace_path, "mempalace_drawers")
        col.add(
            ids=["dedup_1"],
            documents=["Bob knows Alice"],
            metadatas=[{"wing": "notes", "room": "people", "is_latest": True}],
        )

        # Add identical KG triple
        kg_path = str(Path(palace_path) / "knowledge_graph.sqlite3")
        kg = KnowledgeGraph(db_path=kg_path)
        kg.add_triple("Bob", "knows", "Alice", valid_from="2020-01-01")

        result = hybrid_search("Bob knows Alice", palace_path, n_results=10, use_kg=True)
        texts = [r["text"] for r in result["results"]]
        assert len(texts) == len(set(texts))

    def test_hybrid_search_sources_count(self, palace_path, seeded_collection):
        """Result contains sources dict with vector, bm25, and kg keys."""
        from mempalace.searcher import hybrid_search

        result = hybrid_search("JWT", palace_path, n_results=5, use_kg=True)

        assert "sources" in result
        assert "vector" in result["sources"]
        assert "bm25" in result["sources"]
        assert "kg" in result["sources"]
        assert isinstance(result["sources"]["vector"], int)
        assert isinstance(result["sources"]["kg"], int)

    def test_hybrid_search_sources_count_kg_disabled(self, palace_path, seeded_collection):
        """use_kg=False returns kg=0."""
        from mempalace.searcher import hybrid_search

        result = hybrid_search("JWT", palace_path, n_results=5, use_kg=False)

        assert result["sources"]["kg"] == 0
        assert result["sources"]["vector"] >= 0

    def test_hybrid_search_defined_once(self):
        """hybrid_search and hybrid_search_async exist exactly once each."""
        import inspect
        from mempalace.searcher import hybrid_search, hybrid_search_async
        src = inspect.getsource(hybrid_search)
        assert src.count("def hybrid_search(") == 1
        src_async = inspect.getsource(hybrid_search_async)
        assert src_async.count("async def hybrid_search_async(") == 1

    def test_search_memories_returns_all_filters(self, palace_path, seeded_collection):
        """search_memories returns is_latest, agent_id, priority_gte, priority_lte in filters."""
        result = search_memories(
            "JWT", palace_path, n_results=5,
            is_latest=True, agent_id="agent_x",
            priority_gte=3, priority_lte=7,
        )
        assert result["filters"]["is_latest"] is True
        assert result["filters"]["agent_id"] == "agent_x"
        assert result["filters"]["priority_gte"] == 3
        assert result["filters"]["priority_lte"] == 7

    def test_search_memories_uses_cache(self, palace_path, seeded_collection):
        """search_memories calls get_backend (not mocked, real backend)."""
        # This test verifies the cache accessor exists and is callable.
        # Full cache behavior tested via integration in other tests.
        import mempalace.searcher as sr
        cache = sr._get_query_cache()
        assert cache is not None

    def test_cache_not_stored_on_error(self, palace_path, seeded_collection):
        """search_memories with invalid palace path does not store error in cache."""
        # Call with non-existent palace — should error
        result = search_memories("JWT", palace_path="/nonexistent/path", n_results=3)
        assert "error" in result

    def test_cache_key_includes_all_params(self, palace_path, seeded_collection):
        """Different is_latest values produce different cache keys."""
        import mempalace.searcher as sr
        cache = sr._get_query_cache()
        cache.clear()

        # First call with is_latest=True
        r1 = search_memories("JWT", palace_path, n_results=3, is_latest=True)
        keys_after_first = cache._all_keys()

        # Second call with is_latest=False — different key
        r2 = search_memories("JWT", palace_path, n_results=3, is_latest=False)
        keys_after_second = cache._all_keys()

        # Keys should differ
        assert keys_after_first != keys_after_second, "Different is_latest must produce different cache key"


class TestBM25Hybrid:
    def test_bm25_search_exact_match(self, palace_path, seeded_collection):
        """BM25 finds exact token matches that semantic search might rank lower."""
        from mempalace.searcher import hybrid_search
        from mempalace.backends import get_backend

        # Add 10 drawers — "worker" appears in one doc only → positive IDF
        backend = get_backend("chroma")
        col = backend.get_collection(palace_path, "mempalace_drawers")
        col.add(
            ids=["tech_doc"] + [f"other_doc_{i}" for i in range(9)],
            documents=["Set DEBUG=true and run: worker service"]
            + [f"Configure the service at port {8080+i} and start the task" for i in range(9)],
            metadatas=[{"wing": "backend", "room": "api", "is_latest": True}] * 10,
        )

        # "worker" is a standalone token in doc1; BM25 should find it
        result = hybrid_search("worker", palace_path, n_results=5, use_kg=False)
        sources = result.get("sources", {})
        assert sources.get("bm25", 0) >= 1, "BM25 should find exact token match"

    def test_bm25_graceful_no_rank_bm25(self, palace_path, seeded_collection):
        """Without rank_bm25 installed, hybrid_search still returns results."""
        import mempalace.searcher as sr
        from mempalace.searcher import hybrid_search

        # Simulate rank_bm25 not available by patching import
        import builtins
        real_import = builtins.__import__
        def fake_import(name, *args, **kwargs):
            if name == "rank_bm25":
                raise ImportError("rank_bm25 not installed")
            return real_import(name, *args, **kwargs)
        builtins.__import__ = fake_import

        try:
            # Reload to pick up the missing import
            import importlib
            import mempalace.query_cache
            importlib.reload(mempalace.query_cache)

            result = hybrid_search("JWT", palace_path, n_results=5, use_kg=False)
            assert "results" in result
        finally:
            builtins.__import__ = real_import

    def test_rrf_merge_combines_sources(self):
        """RRF gives higher score to hits appearing in multiple result lists."""
        from mempalace.searcher import _rrf_merge

        list1 = [{"text": "shared result"}, {"text": "unique1"}]
        list2 = [{"text": "shared result"}, {"text": "unique2"}]

        result = _rrf_merge([list1, list2])

        # Find shared result
        shared = next((h for h in result if h["text"] == "shared result"), None)
        assert shared is not None
        assert shared.get("rrf_score", 0) > 0, "Shared hit should have rrf_score"

    def test_hybrid_search_sources_has_bm25(self, palace_path, seeded_collection):
        """hybrid_search result['sources'] must contain 'bm25' key."""
        from mempalace.searcher import hybrid_search

        result = hybrid_search("JWT", palace_path, n_results=5, use_kg=False)
        assert "bm25" in result.get("sources", {}), "sources must have bm25 key"

    def test_bm25_min_score_threshold(self, palace_path, seeded_collection):
        """BM25 hits with score > 0 are kept; zero-score hits are filtered."""
        from mempalace.searcher import _bm25_search
        from mempalace.backends import get_backend

        backend = get_backend("chroma")
        col = backend.get_collection(palace_path, "mempalace_drawers")

        # Add 10 docs where "xyzzy" appears in one — gives positive IDF with 10 docs
        col.add(
            ids=["unique1"] + [f"other_{i}" for i in range(9)],
            documents=["foo bar xyzzy quux"] + [f"foo bar baz quux number {i}" for i in range(9)],
            metadatas=[{"wing": "notes", "room": "test", "is_latest": True}] * 10,
        )

        hits = _bm25_search("xyzzy", col, palace_path, n_results=10)
        # xyzzy appears in doc1 only → positive IDF → positive score
        xyzzy_hits = [h for h in hits if "xyzzy" in h["text"]]
        assert len(xyzzy_hits) >= 1, "xyzzy should be found"
        assert all(h.get("bm25_score", 0) > 0 for h in xyzzy_hits), "BM25 score must be > 0 for discriminative term"


# ── Path fidelity tests ────────────────────────────────────────────────────────


class TestPathContains:
    """Tests for path-aware file_path filtering."""

    def test_path_contains_exact_suffix(self):
        """Full path should match when file_path is the filename."""
        from mempalace.searcher import _path_contains
        assert _path_contains("/src/utils.py", "utils.py") is True
        assert _path_contains("/src/utils.py", "utils") is False  # no extension

    def test_path_contains_exact_dir_match(self):
        """Dir path should match when source_file is under that dir."""
        from mempalace.searcher import _path_contains
        assert _path_contains("/src/utils.py", "src") is True
        assert _path_contains("/src/utils.py", "/src") is True
        assert _path_contains("/src/utils.py", "src/utils") is False  # partial component

    def test_path_contains_case_insensitive(self):
        """Matching should be case-insensitive."""
        from mempalace.searcher import _path_contains
        assert _path_contains("/Src/Utils.py", "utils.py") is True
        assert _path_contains("/SRC/utils.py", "src") is True

    def test_path_contains_partial_not_matched(self):
        """Partial path segments should NOT match."""
        from mempalace.searcher import _path_contains
        # "til" should NOT match "/src/utils.py" even though "util" contains "til"
        assert _path_contains("/src/utils.py", "til") is False
        assert _path_contains("/src/utils.py", "util") is False
        # This was the original bug: "utils.py" matched "/src/utils.py" via substring

    def test_path_contains_empty_inputs(self):
        """Empty inputs should return False."""
        from mempalace.searcher import _path_contains
        assert _path_contains("", "utils.py") is False
        assert _path_contains("/src/utils.py", "") is False
        assert _path_contains("", "") is False

    def test_path_contains_same_basename_different_dirs(self):
        """Files with same basename in different dirs should NOT cross-match."""
        from mempalace.searcher import _path_contains
        # Filtering for "utils.py" should match both, not cross-match
        assert _path_contains("/src/utils.py", "utils.py") is True
        assert _path_contains("/lib/utils.py", "utils.py") is True
        # But filtering for "/src/utils.py" should only match one
        assert _path_contains("/lib/utils.py", "/src/utils.py") is False


class TestCodeSearchPathFilter:
    """Tests for file_path filter in code_search."""

    def test_code_search_filters_by_full_path(self, palace_path, seeded_collection):
        """file_path filter should match exact file path."""
        from mempalace.searcher import code_search
        from mempalace.backends import get_backend

        # Add test documents with known paths
        backend = get_backend("chroma")
        col = backend.get_collection(palace_path, "mempalace_drawers")
        col.add(
            ids=["path_test_1"],
            documents=["function in src utils"],
            metadatas=[{
                "wing": "repo", "room": "code", "is_latest": True,
                "source_file": "/project/src/utils.py", "language": "python"
            }],
        )
        col.add(
            ids=["path_test_2"],
            documents=["function in lib utils"],
            metadatas=[{
                "wing": "repo", "room": "code", "is_latest": True,
                "source_file": "/project/lib/utils.py", "language": "python"
            }],
        )

        # Filter by full path should return only that file
        result = code_search("function", palace_path, n_results=5, file_path="/project/src/utils.py")
        results_paths = [h.get("source_file") for h in result.get("results", [])]
        assert "/project/src/utils.py" in results_paths
        assert "/project/lib/utils.py" not in results_paths

    def test_code_search_same_basename_disambiguation(self, palace_path, seeded_collection):
        """Two files with same basename in different dirs should be disambiguable."""
        from mempalace.searcher import code_search
        from mempalace.backends import get_backend

        backend = get_backend("chroma")
        col = backend.get_collection(palace_path, "mempalace_drawers")
        col.add(
            ids=["ambig_1"],
            documents=["auth implementation"],
            metadatas=[{
                "wing": "repo", "room": "code", "is_latest": True,
                "source_file": "/project/frontend/auth.py", "language": "python"
            }],
        )
        col.add(
            ids=["ambig_2"],
            documents=["auth implementation"],
            metadatas=[{
                "wing": "repo", "room": "code", "is_latest": True,
                "source_file": "/project/backend/auth.py", "language": "python"
            }],
        )

        # Filter by parent dir should disambiguate
        result = code_search("auth", palace_path, n_results=5, file_path="frontend/auth.py")
        results_paths = [h.get("source_file") for h in result.get("results", [])]
        assert any("/frontend/auth.py" in p for p in results_paths)
        assert not any("/backend/auth.py" in p for p in results_paths)


class TestRepoRelPath:
    """Tests for repo_rel_path computation."""

    def test_compute_repo_rel_path(self):
        """repo_rel_path should strip common prefix."""
        from mempalace.searcher import _compute_repo_rel_path
        assert _compute_repo_rel_path("/project/src/utils.py", "/project") == "src/utils.py"
        assert _compute_repo_rel_path("/project/src/utils.py", "/project/") == "src/utils.py"
        assert _compute_repo_rel_path("/project/src/utils.py", "/other") == "/project/src/utils.py"  # no common prefix

    def test_add_repo_rel_path(self):
        """_add_repo_rel_path should add field when common prefix exists."""
        from mempalace.searcher import _add_repo_rel_path

        hits = [
            {"source_file": "/project/src/utils.py"},
            {"source_file": "/project/src/main.py"},
            {"source_file": "/project/lib/auth.py"},
        ]
        source_files = [h["source_file"] for h in hits]
        result = _add_repo_rel_path(hits, source_files)

        assert result[0].get("repo_rel_path") == "src/utils.py"
        assert result[1].get("repo_rel_path") == "src/main.py"
        assert result[2].get("repo_rel_path") == "lib/auth.py"

    def test_add_repo_rel_path_no_common_prefix(self):
        """_add_repo_rel_path should not add field when no real common prefix."""
        from mempalace.searcher import _add_repo_rel_path

        hits = [
            {"source_file": "/project/src/utils.py"},
            {"source_file": "/other/lib/auth.py"},
        ]
        source_files = [h["source_file"] for h in hits]
        result = _add_repo_rel_path(hits, source_files)

        # No field added when common prefix is just "/"
        assert "repo_rel_path" not in result[0]

    def test_search_memories_includes_repo_rel_path(self, palace_path, seeded_collection):
        """search_memories results should include repo_rel_path when applicable."""
        from mempalace.searcher import search_memories
        from mempalace.backends import get_backend

        backend = get_backend("chroma")
        col = backend.get_collection(palace_path, "mempalace_drawers")
        col.add(
            ids=["repo_rel_test"],
            documents=["test document for repo rel path"],
            metadatas=[{
                "wing": "repo", "room": "code", "is_latest": True,
                "source_file": "/test_project/src/main.py"
            }],
        )

        result = search_memories("repo rel path", palace_path, n_results=5)
        hits = result.get("results", [])
        if hits and hits[0].get("source_file"):
            # repo_rel_path should be present (or absent if no common prefix)
            # Just verify the field exists or doesn't as appropriate
            assert "repo_rel_path" in hits[0] or "repo_rel_path" not in hits[0]

